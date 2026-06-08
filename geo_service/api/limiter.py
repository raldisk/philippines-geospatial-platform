"""
geo_service/api/limiter.py

Singleton SlowAPI rate limiter instance.
Extracted from api/main.py to break the circular import:
    api/main.py → routes/*.py → api/main.py (circular)

All route modules import limiter from here; main.py also imports from here.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
