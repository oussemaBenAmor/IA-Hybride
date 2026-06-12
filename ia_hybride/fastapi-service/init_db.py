import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app.db.postgres import init_db
from app.db.vector_store import seed_cases

if __name__ == "__main__":
    print("🔧 Initialisation de la base de données...")
    init_db()
    print("🌱 Peuplement du vector store...")
    seed_cases()
    print("✅ Base de données prête !")