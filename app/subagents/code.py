"""CodeAgent: генератор–критик. Дешёвая модель пишет код, сильная ревьюит, до 3 итераций."""

import re
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent

from ..config import settings
from ..llm import cheap_model, strong_model

MAX_ITERATIONS = 3
FILE_CAP = 8000  # сколько символов файла показываем ревьюеру


class CodeFile(BaseModel):
    path: str
    content: str


class CodeBundle(BaseModel):
    files: list[CodeFile]
    notes: str  # как запускать, что сделано


class Review(BaseModel):
    approved: bool
    issues: list[str]


_generator = Agent(
    cheap_model(),
    output_type=CodeBundle,
    system_prompt=(
        "Ты пишешь рабочий код по заданию. Полные файлы, без заглушек и '...'. "
        "Минимум зависимостей. Если нужны зависимости — добавь requirements.txt/package.json. "
        "В notes: как запустить и что сделано. Комментарии в коде — только по делу."
    ),
)

_critic = Agent(
    strong_model(),
    output_type=Review,
    system_prompt=(
        "Ты — строгий код-ревьюер. Проверь код на: баги, незавершённые места, ошибки логики, "
        "проблемы безопасности, несоответствие заданию. "
        "approved=true только если код реально готов к запуску. "
        "issues — конкретные проблемы с указанием файла (по-русски)."
    ),
)


def _dump(files: list[CodeFile]) -> str:
    return "\n\n".join(f"=== {f.path} ===\n{f.content[:FILE_CAP]}" for f in files)


def _safe_path(base: Path, rel: str) -> Path | None:
    rel = re.sub(r"^[/\\]+", "", rel.replace("..", ""))
    path = (base / rel).resolve()
    return path if str(path).startswith(str(base.resolve())) else None


async def run(task_id: int, description: str) -> str:
    bundle = (await _generator.run(f"Задание:\n{description}")).output

    review = Review(approved=False, issues=[])
    for _ in range(MAX_ITERATIONS):
        review = (
            await _critic.run(f"Задание:\n{description}\n\nКод:\n{_dump(bundle.files)}")
        ).output
        if review.approved:
            break
        fix_prompt = (
            f"Задание:\n{description}\n\nТвой код:\n{_dump(bundle.files)}\n\n"
            "Ревьюер нашёл проблемы:\n" + "\n".join(f"- {i}" for i in review.issues) +
            "\n\nИсправь все проблемы и верни полный обновлённый набор файлов."
        )
        bundle = (await _generator.run(fix_prompt)).output

    out_dir = Path(settings.data_dir) / "code" / f"task_{task_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in bundle.files:
        path = _safe_path(out_dir, f.path)
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f.content, encoding="utf-8")
        saved.append(f.path)

    verdict = (
        "✅ ревью пройдено"
        if review.approved
        else "⚠️ остались замечания:\n" + "\n".join(f"- {i}" for i in review.issues[:5])
    )
    return (
        f"Код готов: {len(saved)} файл(ов) в data/code/task_{task_id}/\n"
        f"Файлы: {', '.join(saved)}\n"
        f"Ревью (сильная модель): {verdict}\n\n"
        f"Заметки: {bundle.notes}"
    )
