from sqlalchemy import Column, Integer, Text, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

class JobsModel(Base):
    '''Model for storing freelance job listings
    '''
    __tablename__ = 'jobs'
    __table_args__ = {'schema': 'consulting'}
    id = Column(Integer, primary_key=True)
    url = Column(Text, nullable=True)
    time_created = Column(DateTime(timezone=True), server_default=func.now())
    title = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    budget = Column(Text, nullable=True)
    status = Column(Text, nullable=True)
    proposal = Column(Text, nullable=True)

