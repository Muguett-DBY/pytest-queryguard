from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine("sqlite:///app.db")
sessions = sessionmaker(engine)

async_engine = create_async_engine("sqlite+aiosqlite:///app.db")
async_sessions = async_sessionmaker(async_engine, expire_on_commit=False)
