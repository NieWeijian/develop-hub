from setuptools import find_packages, setup


setup(
    name="develop-hub-server",
    version="0.1.0",
    description="175 control plane for Codex-driven software development workflows",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.10",
    install_requires=[
        "alembic>=1.13,<2",
        "fastapi>=0.115,<1",
        "psycopg[binary]>=3.2,<4",
        "pydantic>=2.9,<3",
        "sqlalchemy>=2.0,<3",
        "uvicorn[standard]>=0.30,<1",
    ],
    extras_require={
        "dev": ["httpx>=0.27,<1", "pytest>=8.3,<9", "pytest-asyncio>=0.24,<1"],
    },
    entry_points={"console_scripts": ["develop-hub=develop_hub.main:run"]},
)
