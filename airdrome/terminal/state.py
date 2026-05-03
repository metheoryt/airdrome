from dataclasses import dataclass

from sqlmodel import Session


@dataclass
class AppState:
    session: Session
    dry_run: bool
