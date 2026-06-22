from setuptools import setup, find_packages

setup(
    name="survey-check",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "pandas>=2.0.0",
        "openpyxl>=3.1.0",
        "click>=8.1.0",
    ],
    entry_points={
        "console_scripts": [
            "survey-check=survey_check.cli:main",
        ],
    },
    python_requires=">=3.9",
)
