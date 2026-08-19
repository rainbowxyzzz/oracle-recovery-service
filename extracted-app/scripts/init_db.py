import asyncio

from recovery_service.db.session import init_db


def main():
    asyncio.run(init_db())
    print("Database initialized.")


if __name__ == "__main__":
    main()
