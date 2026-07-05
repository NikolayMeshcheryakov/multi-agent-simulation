
from __future__ import annotations

import argparse
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from google import genai
from dotenv import load_dotenv

load_dotenv()  # подхватывает .env из текущей рабочей директории, если он есть

# --------------------------------------------------------------------------- #
# Конфигурация курса
# --------------------------------------------------------------------------- #

TOPIC = "Portfolio Investing"

LESSON_TITLES = [
    "Diversification",
    "Risk vs Return",
    "Asset Allocation",
    "Portfolio Rebalancing",
    "Correlation",
    "Dollar-Cost Averaging",
    "Expected Portfolio Return",
    "Sharpe Ratio",
    "Hedging Basics",
    "Building a Complete Investment Plan",
]


# Список моделей в порядке предпочтения. Если основная модель падает после всех
# ретраев (перегрузка / 503 / временная недоступность), код автоматически
# переключается на следующую в списке — вместо того чтобы останавливать весь диалог.
_default_models = "gemma-4-26b-a4b-it,gemini-2.5-flash-lite,gemini-2.0-flash-001,gemini-2.0-flash"
MODEL_CANDIDATES = [
    m.strip() for m in os.environ.get("MENTOR_STUDENT_MODELS", _default_models).split(",") if m.strip()
]
MAX_TURNS_PER_LESSON = 4
RETRY_DELAYS = [3, 6, 9, 12, 15]  # секунды на каждую модель, как в предыдущих версиях


# --------------------------------------------------------------------------- #
# Профили студентов
# --------------------------------------------------------------------------- #

@dataclass
class StudentProfile:
    name: str
    age: int
    occupation: str
    capital: int
    goal: str
    personality: str
    bluff_lessons: list[int] = field(default_factory=list)  # явный список уроков для блафа
    bluff_count: int = 0                                     # либо "N раз, код сам решит когда"
    makes_small_errors: bool = False                         # Alex-стиль: систематическая мелкая ошибка
    seed: Optional[int] = None                                # для воспроизводимости случайного расписания блафов

    def resolved_bluff_lessons(self, num_lessons: int) -> list[int]:
        """Возвращает финальный список уроков, на которых студент блефует.
        Решение принимается ЗДЕСЬ, в коде, один раз в начале диалога —
        модель никогда не должна сама выбирать, когда ей "разрешено" сблефовать.
        """
        if self.bluff_lessons:
            return sorted(l for l in self.bluff_lessons if 1 <= l <= num_lessons)
        if self.bluff_count:
            rng = random.Random(self.seed)
            k = min(self.bluff_count, num_lessons)
            return sorted(rng.sample(range(1, num_lessons + 1), k=k))
        return []


NIKOLAY = StudentProfile(
    name="Nikolay",
    age=28,
    occupation="Data Analyst, fintech",
    capital=10_000,
    goal="достичь финансовой независимости за 15 лет",
    personality=(
        "умный, нетерпеливый, ценит точные цифры и логику, слегка самоуверен "
        "по поводу своих аналитических способностей"
    ),
    bluff_lessons=[3, 6, 8],
)

ALEX = StudentProfile(
    name="Alex",
    age=35,
    occupation="Sales manager",
    capital=25_000,
    goal="выйти на пенсию в 50 лет",
    personality=(
        'самоуверенный, часто говорит "I already knew that", любит ссылаться на '
        "реальные тикеры вроде $NVDA и $TSLA, не любит "
        '"копаться в деталях"'
    ),
    bluff_count=3,
    makes_small_errors=True,
    seed=42,
)


# --------------------------------------------------------------------------- #
# Состояние диалога — единственный источник правды по прогрессу курса
# --------------------------------------------------------------------------- #

@dataclass
class DialogueState:
    num_lessons: int
    current_lesson: int = 1
    turns_on_current_lesson: int = 0
    total_turns: int = 0
    bluffs_scheduled: list[int] = field(default_factory=list)
    bluffs_triggered_on: list[int] = field(default_factory=list)
    bluff_pending_admission: bool = False
    course_complete: bool = False
    transcript: list[dict] = field(default_factory=list)

    def lesson_in_progress_too_long(self) -> bool:
        return self.turns_on_current_lesson >= MAX_TURNS_PER_LESSON

    def is_bluff_turn(self) -> bool:
        return (
            self.current_lesson in self.bluffs_scheduled
            and self.current_lesson not in self.bluffs_triggered_on
        )

    def lessons_completed(self) -> int:
        if self.course_complete:
            return self.num_lessons
        return max(self.current_lesson - 1, 0)


# --------------------------------------------------------------------------- #
# Вызов модели: ретраи + никакой тихой деградации
# --------------------------------------------------------------------------- #

class ModelCallError(RuntimeError):
    pass


def call_model(client: "genai.Client", prompt: str, label: str) -> str:
    """Пробует каждую модель из MODEL_CANDIDATES по очереди. На каждой модели —
    полный цикл ретраев с нарастающей паузой. Переходит к следующей модели только
    если текущая не ответила ни разу за все попытки (перегрузка / 503 / квота).
    Останавливается с явной ошибкой, только если ВСЕ модели из списка не сработали.
    """
    last_err: Optional[Exception] = None
    for model_name in MODEL_CANDIDATES:
        for attempt, delay in enumerate([0] + RETRY_DELAYS, start=1):
            if delay:
                logging.warning(
                    "[%s][%s] попытка %d/%d, жду %ds после предыдущей ошибки",
                    label, model_name, attempt, len(RETRY_DELAYS) + 1, delay,
                )
                time.sleep(delay)
            try:
                resp = client.models.generate_content(model=model_name, contents=prompt)
                text = (getattr(resp, "text", "") or "").strip()
                if not text:
                    raise ModelCallError("Пустой ответ от API")
                return text
            except Exception as e:  # noqa: BLE001 — намеренно широкий catch: сетевые/5xx/квота-ошибки
                last_err = e
                logging.error("[%s][%s] ошибка вызова API: %s", label, model_name, e)
        logging.warning(
            "[%s] модель %s не ответила после %d попыток, пробую следующую модель из списка",
            label, model_name, len(RETRY_DELAYS) + 1,
        )
    raise ModelCallError(
        f"Не удалось получить ответ ({label}) ни от одной из моделей {MODEL_CANDIDATES}: {last_err}"
    )


# --------------------------------------------------------------------------- #
# Санитайзер и парсер служебной STATE-метки
# --------------------------------------------------------------------------- #

ROLE_PREFIX_RE = re.compile(r"^\s*(MENTOR|STUDENT)\s*[:\-]\s*", re.IGNORECASE)
STATE_RE = re.compile(r"STATE:\s*lesson\s*=\s*(\d+)\s+advance\s*=\s*(true|false)", re.IGNORECASE)


def sanitize_response(text: str) -> str:
    return ROLE_PREFIX_RE.sub("", text.strip(), count=1)


def parse_state(raw_text: str) -> Optional[tuple[int, bool]]:
    m = STATE_RE.search(raw_text)
    if not m:
        return None
    return int(m.group(1)), m.group(2).lower() == "true"


def strip_state_line(raw_text: str) -> str:
    text = STATE_RE.sub("", raw_text)
    text = re.sub(r"\n-{3,}\s*$", "", text.strip())
    return text.strip()


# --------------------------------------------------------------------------- #
# Промпты
# --------------------------------------------------------------------------- #

MENTOR_SYSTEM_PROMPT = """You are an expert investment mentor teaching a {num_lessons}-lesson course on {topic}, one-on-one with a student.

FIXED CURRICULUM (CRITICAL — do not substitute, skip, merge, or invent different topics):
{syllabus}
Each lesson MUST cover exactly the topic listed above for its number — not a related but different topic of your own choosing.

VERIFICATION RULES (CRITICAL):
- NEVER accept "yes I did it" without specific numbers.
- If the answer is vague — refuse to advance, ask for specifics.
- If the student bluffed and admitted it — accept the admission and move on.
- Do not spend more than {max_turns} exchanges on a single lesson (your current counter is given below; if the limit is reached, you MUST advance).

TECHNIQUES:
- SPECIFICS: "what exact numbers did you get?"
- FRICTION: "what was confusing?"
- TRANSFER: give a new scenario requiring the same skill (a Transfer Test).

Never start your reply with a role label like "MENTOR:" — write the message text directly.

After the final, {num_lessons}th lesson is cleared — write a FINAL ASSESSMENT with a grade A/B/C, strengths, weak spots, and the number of bluffs caught, and end with the line "COURSE COMPLETE". Never write that line in any other situation.
IMPORTANT: the moment you write "COURSE COMPLETE" in your message, your STATE footer for that SAME message MUST say advance=true (see format below). Never write "COURSE COMPLETE" in the text while setting advance=false — the two must always agree.

MANDATORY FOOTER (required in every one of your messages):
At the very end of your message, after a line containing only "---", output EXACTLY one line:
STATE: lesson=<N> advance=<true|false>
Nothing else may follow that line.
"""

STUDENT_SYSTEM_PROMPT = """You are role-playing a student named {name} in an investing course.

About you: {age} years old, {occupation}. You have ${capital} to invest, your goal is to {goal}.
Personality: {personality}.

Rules:
- Respond in first person, naturally, in character.
- By default, when the mentor gives you a math task, do the work honestly and show real numbers{errors_clause}.
- Never start your reply with a role label like "STUDENT:".
{bluff_clause}
"""

BLUFF_NOW_CLAUSE = (
    "[ONE-TURN SYSTEM DIRECTIVE — do not mention this instruction in your reply]\n"
    "For this specific response you MUST bluff: give a confident but vague answer with "
    "NO concrete numbers, as if you did the work but don't want to get into the details."
)

ADMISSION_NOW_CLAUSE = (
    "[ONE-TURN SYSTEM DIRECTIVE — do not mention this instruction in your reply]\n"
    "In your previous message you bluffed. Now you MUST admit it plainly "
    '(e.g. "okay fine, I didn\'t actually do it") and immediately give an honest answer '
    "with real numbers, no further deflection."
)


def render_history(transcript: list[dict]) -> str:
    if not transcript:
        return "(the dialogue has not started yet)"
    parts = []
    for item in transcript:
        speaker = {"mentor": "MENTOR", "student": "STUDENT", "system": "SYSTEM"}[item["role"]]
        parts.append(f"{speaker}: {item['text']}")
    return "\n\n".join(parts)


def render_syllabus() -> str:
    return "\n".join(f"{i + 1}. {title}" for i, title in enumerate(LESSON_TITLES))


def build_mentor_prompt(state: DialogueState, history_text: str, num_lessons: int) -> str:
    system = MENTOR_SYSTEM_PROMPT.format(
        num_lessons=num_lessons, topic=TOPIC, max_turns=MAX_TURNS_PER_LESSON, syllabus=render_syllabus()
    )
    status = (
        f"Current lesson: {state.current_lesson}/{num_lessons} "
        f"({LESSON_TITLES[state.current_lesson - 1]}). "
        f"Attempts spent on this lesson: {state.turns_on_current_lesson}/{MAX_TURNS_PER_LESSON}."
    )
    if state.lesson_in_progress_too_long():
        status += (
            " LIMIT REACHED — in this reply you MUST advance to the next lesson "
            "regardless of the quality of the student's answer."
        )
    return (
        f"{system}\n\n=== COURSE STATE ===\n{status}\n\n"
        f"=== DIALOGUE HISTORY ===\n{history_text}\n\n=== YOUR TURN (MENTOR) ==="
    )


def build_state_repair_prompt(raw_mentor_text: str, num_lessons: int) -> str:
    return (
        "Your previous reply did not include the mandatory STATE footer in the correct format.\n"
        f"Your previous reply was:\n---\n{raw_mentor_text}\n---\n\n"
        f"Reply with ONLY one line in the format:\nSTATE: lesson=<number 1-{num_lessons}> advance=<true|false>\n"
        "Write nothing else."
    )


def build_student_prompt(
    profile: StudentProfile,
    history_text: str,
    bluff_now: bool,
    admission_now: bool,
) -> str:
    errors_clause = ""
    if profile.makes_small_errors:
        errors_clause = (
            ", but consistently make one small arithmetic slip (usually off by about $100) — "
            "that's part of your character"
        )

    if bluff_now:
        bluff_clause = BLUFF_NOW_CLAUSE
    elif admission_now:
        bluff_clause = ADMISSION_NOW_CLAUSE
    else:
        bluff_clause = ""

    system = STUDENT_SYSTEM_PROMPT.format(
        name=profile.name,
        age=profile.age,
        occupation=profile.occupation,
        capital=profile.capital,
        goal=profile.goal,
        personality=profile.personality,
        errors_clause=errors_clause,
        bluff_clause=bluff_clause,
    )
    return f"{system}\n\n=== DIALOGUE HISTORY ===\n{history_text}\n\n=== YOUR TURN ({profile.name.upper()}) ==="


# --------------------------------------------------------------------------- #
# Основной цикл диалога
# --------------------------------------------------------------------------- #

def run_dialogue(
    client: "genai.Client", profile: StudentProfile, num_lessons: int = 10, max_total_turns: int = 400
) -> DialogueState:
    state = DialogueState(num_lessons=num_lessons)
    state.bluffs_scheduled = profile.resolved_bluff_lessons(num_lessons)
    logging.info("[%s] запланированные уроки для блафа: %s", profile.name, state.bluffs_scheduled)

    turn_role = "mentor"  # ментор ходит первым

    try:
        while not state.course_complete and state.total_turns < max_total_turns:
            history_text = render_history(state.transcript)

            if turn_role == "mentor":
                prompt = build_mentor_prompt(state, history_text, num_lessons)
                raw = call_model(client, prompt, label=f"MENTOR turn {state.total_turns + 1}")

                parsed = parse_state(raw)
                if parsed is None:
                    logging.warning("STATE-метка не найдена, прошу модель повторить только её")
                    repair_raw = call_model(
                        client, build_state_repair_prompt(raw, num_lessons), label="MENTOR state-repair"
                    )
                    parsed = parse_state(repair_raw)
                if parsed is None:
                    logging.error("STATE так и не распознана — считаю advance=false (прогресс не теряем)")
                    parsed = (state.current_lesson, False)

                _parsed_lesson, advance = parsed
                clean_text = strip_state_line(sanitize_response(raw))
                state.transcript.append({"role": "mentor", "text": clean_text, "turn": state.total_turns + 1})

                # Страховка от рассинхрона текста и STATE: если модель уже написала
                # "COURSE COMPLETE" на последнем уроке, но забыла поставить advance=true —
                # код доверяет этому явному текстовому маркеру именно ЗДЕСЬ (и только на
                # последнем уроке, так что правило "не раньше 10-го урока" не нарушается).
                if (
                    not advance
                    and state.current_lesson == num_lessons
                    and "COURSE COMPLETE" in raw.upper()
                ):
                    advance = True
                    logging.info("Текст содержит COURSE COMPLETE на последнем уроке — синхронизирую advance=true")

                forced = False
                if not advance and state.lesson_in_progress_too_long():
                    advance = True
                    forced = True
                    logging.warning(
                        "Лимит попыток на уроке %d исчерпан — форсирую переход (решение кода, не модели)",
                        state.current_lesson,
                    )

                if advance:
                    if state.current_lesson < num_lessons:
                        state.current_lesson += 1
                        state.turns_on_current_lesson = 0
                    else:
                        # current_lesson == num_lessons и advance=true — и только в этом
                        # случае курс может завершиться. Это проверяется кодом, а не текстом
                        # "COURSE COMPLETE" от модели.
                        state.course_complete = True
                else:
                    state.turns_on_current_lesson += 1

                if forced:
                    logging.info("FORCED ADVANCE -> lesson %d", state.current_lesson)

                turn_role = "student"

            else:
                bluff_now = state.is_bluff_turn()
                admission_now = state.bluff_pending_admission and not bluff_now

                prompt = build_student_prompt(profile, history_text, bluff_now, admission_now)
                raw = call_model(client, prompt, label=f"STUDENT turn {state.total_turns + 1}")
                clean_text = sanitize_response(raw)
                state.transcript.append({"role": "student", "text": clean_text, "turn": state.total_turns + 1})

                if bluff_now:
                    state.bluffs_triggered_on.append(state.current_lesson)
                    state.bluff_pending_admission = True
                elif admission_now:
                    state.bluff_pending_admission = False

                turn_role = "mentor"

            state.total_turns += 1

    except ModelCallError as e:
        logging.error("Диалог прерван из-за ошибки API: %s", e)
        state.transcript.append(
            {"role": "system", "text": f"[ДИАЛОГ ПРЕРВАН ИЗ-ЗА ОШИБКИ API: {e}]", "turn": state.total_turns + 1}
        )

    if state.total_turns >= max_total_turns and not state.course_complete:
        logging.error("Достигнут глобальный лимит ходов (%d) — останавливаюсь без завершения курса", max_total_turns)

    return state


# --------------------------------------------------------------------------- #
# Сохранение результатов
# --------------------------------------------------------------------------- #

def save_dialogue_md(state: DialogueState, profile: StudentProfile, run_number: int, out_dir: Path) -> Path:
    lines = [
        f"# Mentor & Student Dialogue — Run {run_number}",
        f"## Student: {profile.name} | Topic: {TOPIC}",
        "",
        f"Generated: {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"**Stats:** Lessons: {state.lessons_completed()}/{state.num_lessons} | "
        f"Bluffs caught: {len(state.bluffs_triggered_on)} | "
        f"Complete: {'Yes' if state.course_complete else 'No'}",
        "",
        "---",
        "",
    ]
    for item in state.transcript:
        speaker = {"mentor": "MENTOR", "student": "STUDENT", "system": "SYSTEM"}[item["role"]]
        lines += [f"### [{speaker}] (Turn {item['turn']})", "", item["text"], "", "---", ""]

    path = out_dir / f"dialogue_run{run_number}_{profile.name.lower()}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def save_summary_md(runs: list[tuple[int, StudentProfile, DialogueState]], out_dir: Path) -> Path:
    lines = ["# Simulation Summary", "", f"Generated: {datetime.now():%Y-%m-%d %H:%M}", "", "---", ""]
    for run_number, profile, state in runs:
        lines += [
            f"## Run {run_number}: {profile.name}",
            "",
            f"- Lessons completed: {state.lessons_completed()}/{state.num_lessons}",
            f"- Bluffs caught: {len(state.bluffs_triggered_on)}",
            f"- Course complete: {'Yes' if state.course_complete else 'No'}",
            "",
        ]
    path = out_dir / "summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def list_available_models(client: "genai.Client") -> None:
    """Печатает id моделей, реально доступных с этим ключом, и какие методы они поддерживают.
    Полезно, когда generateContent падает с 404/500 и непонятно, правильный ли указан id модели.
    """
    print("Доступные модели для этого API-ключа:")
    for m in client.models.list():
        methods = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None)
        print(f"  - {m.name}   (methods: {methods})")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Mentor/Student dialogue simulator (Portfolio Investing)")
    parser.add_argument("--out-dir", default="./output")
    parser.add_argument("--max-total-turns", type=int, default=400)
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="Вывести список моделей, доступных для этого ключа, и выйти (ничего не запускать)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    api_key = "AIzaSyAwvq7rOvCnw7vBWBm-G-PB2cMaONC5Sfs"
    if not api_key:
        raise SystemExit(
            "Не найден GOOGLE_API_KEY. Создай файл .env рядом со скриптом с строкой:\n"
            "GOOGLE_API_KEY=твой_ключ"
        )
    client = genai.Client(api_key=api_key)

    if args.list_models:
        list_available_models(client)
        return

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs: list[tuple[int, StudentProfile, DialogueState]] = []
    for run_number, profile in enumerate([NIKOLAY, ALEX], start=1):
        logging.info("=== Запуск диалога: %s (run %d) ===", profile.name, run_number)
        state = run_dialogue(client, profile, num_lessons=len(LESSON_TITLES), max_total_turns=args.max_total_turns)
        path = save_dialogue_md(state, profile, run_number, out_dir)
        logging.info("Сохранено: %s", path)
        runs.append((run_number, profile, state))

    summary_path = save_summary_md(runs, out_dir)
    logging.info("Готово. Summary: %s", summary_path)


if __name__ == "__main__":
    main()
