from dataclasses import dataclass

from sqlalchemy.orm import Session


@dataclass
class AppState:
    session: Session
    dry_run: bool
