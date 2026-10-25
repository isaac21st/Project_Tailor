"""Settings: where personal data lives and how to reach the model.

The code folder holds no personal data. Everything private (profile, job files,
generated resumes, browser sessions) lives under ``data_dir``, which defaults to
a folder outside OneDrive.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = Path.home() / "project_tailor_data"


@dataclass
class ModelSettings:
    kind: str = "stub"  # "http" | "shell" | "stub"
    base_url: str = "http://localhost:11434/v1"  # OpenAI-compatible endpoint (kind="http")
    model: str = "qwen3:8b"
    command: list[str] = field(default_factory=list)  # kind="shell": prompt is sent on stdin
    timeout_s: float = 600.0
    think: bool = False  # Qwen3 thinks before answering by default: slow, and it can use up the whole budget


@dataclass
class Settings:
    data_dir: Path = DEFAULT_DATA_DIR
    model: ModelSettings = field(default_factory=ModelSettings)

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def browsers_dir(self) -> Path:
        return self.data_dir / "browsers"

    def ensure_dirs(self) -> None:
        for d in (self.profile_dir, self.output_dir, self.tmp_dir, self.browsers_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from an explicit path, $TAILOR_CONFIG, or <project>/config.toml."""
    candidates = [path, os.environ.get("TAILOR_CONFIG"), PROJECT_ROOT / "config.toml"]
    for c in candidates:
        if c and Path(c).is_file():
            raw = tomllib.loads(Path(c).read_text(encoding="utf-8"))
            break
    else:
        raw = {}
    model = ModelSettings(**raw.get("model", {}))
    data_dir = Path(raw["data_dir"]).expanduser() if "data_dir" in raw else DEFAULT_DATA_DIR
    return Settings(data_dir=data_dir, model=model)


def apply_environment(settings: Settings) -> None:
    """Point Playwright at the browsers stored under data_dir (must run before launching)."""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(settings.browsers_dir))
