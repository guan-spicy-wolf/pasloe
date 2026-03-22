"""Pytest configuration for test suite."""
import os

# Set test database configuration before any imports
os.environ["DB_TYPE"] = "sqlite"
os.environ["SQLITE_PATH"] = ":memory:"
os.environ["API_KEY"] = ""  # disable auth in tests
