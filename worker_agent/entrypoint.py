"""Worker-agent process entrypoint kept separate from the agent implementation."""

from .app import WorkerAgent
from .config import WorkerConfig


def main() -> None:
    WorkerAgent(WorkerConfig.load()).run()


if __name__ == "__main__":
    main()
