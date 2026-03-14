"""
═══════════════════════════════════════════════════════════════
SHARED DATABASE UTILITIES
═══════════════════════════════════════════════════════════════
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool
from contextlib import contextmanager

# ═══════════════════════════════════════════════════════════════
# DATABASE CONNECTION
# ═══════════════════════════════════════════════════════════════

def get_database_url() -> str:
    """Get PostgreSQL database URL from environment"""
    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    
    if not all([db_name, db_user, db_password]):
        raise ValueError("Database credentials not configured")
    
    return f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"


def get_db_engine():
    """Get SQLAlchemy engine with connection pooling"""
    database_url = get_database_url()
    
    engine = create_engine(
        database_url,
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,  # Verify connections
        pool_recycle=3600,   # Recycle connections after 1 hour
        echo=False,
    )
    
    return engine


@contextmanager
def get_db_connection():
    """Context manager for database connections"""
    engine = get_db_engine()
    conn = engine.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def execute_query(query: str, params: dict = None):
    """Execute a query and return results"""
    with get_db_connection() as conn:
        result = conn.execute(query, params or {})
        return result.fetchall()


def execute_update(query: str, params: dict = None):
    """Execute an update/insert query"""
    with get_db_connection() as conn:
        result = conn.execute(query, params or {})
        return result.rowcount