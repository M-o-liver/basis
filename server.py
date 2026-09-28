"""Compatibility launcher for the local BASIS research service."""
import os
from basislab.config import Config
from basislab.service import serve

if __name__ == '__main__':
    serve(db=os.environ.get('BASIS_DB', 'data/basis.sqlite3'), port=int(os.environ.get('BASIS_PORT', '8765')),
          config=Config.load(os.environ.get('BASIS_CONFIG')))
