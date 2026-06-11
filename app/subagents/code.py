"""CodeAgent: генератор–критик. Дешёвая модель пишет код, сильная ревьюит, до 3 итераций."""

import re
from pathlib import Path

from pydantic import BaseModel

from ..agents_registry import AgentSpec, build, register
from ..config import settings

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


GENERATOR_PROMPT = (
    "Ты пишешь рабочий код по заданию. Полные файлы, без заглушек и '...'. "
    "Минимум зависимостей. Если нужны зависимости — добавь requirements.txt/package.json. "
    "В notes: как запустить и что сделано. Комментарии в коде — только по делу."
)

CRITIC_PROMPT = (
    "Ты — строгий код-ревьюер. Проверь код на: баги, незавершённые места, ошибки логики, "
    "проблемы безопасности, несоответствие заданию. "
    "approved=true только если код реально готов к запуску. "
    "issues — конкретные проблемы с указанием файла (по-русски)."
)

register(
    AgentSpec(
        name="code_generator",
        title="Код: генератор",
        tier="cheap",
        prompt=GENERATOR_PROMPT,
        description="Пишет код по заданию (дешёвая модель).",
        output_type=CodeBundle,
    )
)

register(
    AgentSpec(
        name="code_critic",
        title="Код: ревьюер",
        tier="strong",
        prompt=CRITIC_PROMPT,
        description="Ревьюит код сильной моделью, до 3 итераций исправлений.",
        output_type=Review,
    )
)


def _dump(files: list[CodeFile]) -> str:
    return "\n\n".join(f"=== {f.path} ===\n{f.content[:FILE_CAP]}" for f in files)


def _safe_path(base: Path, rel: str) -> Path | None:
    rel = re.sub(r"^[/\\]+", "", rel.replace("..", ""))
    path = (base / rel).resolve()
    return path if str(path).startswith(str(base.resolve())) else None


async def run(task_id: int, description: str) -> str:
    generator, generator_on = await build("code_generator")
    critic, critic_on = await build("code_critic")
    if not generator_on:
        return "Кодовый агент выключен в админке."

    bundle = (await generator.run(f"Задание:\n{description}")).output

    review = Review(approved=not critic_on, issues=[])
    if critic_on:
        for _ in range(MAX_ITERATIONS):
            review = (
                await critic.run(f"Задание:\n{description}\n\nКод:\n{_dump(bundle.files)}")
            ).output
            if review.approved:
                break
            fix_prompt = (
                f"Задание:\n{description}\n\nТвой код:\n{_dump(bundle.files)}\n\n"
                "Ревьюер нашёл проблемы:\n" + "\n".join(f"- {i}" for i in review.issues) +
                "\n\nИсправь все проблемы и верни полный обновлённый набор файлов."
            )
            bundle = (await generator.run(fix_prompt)).output

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
