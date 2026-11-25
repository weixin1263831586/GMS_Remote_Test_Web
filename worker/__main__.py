"""Allow ``python -m worker`` to use the dedicated entrypoint."""

from .entrypoint import main


if __name__ == "__main__":
    main()
