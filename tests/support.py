from pathlib import Path
import tempfile
import unittest
from payment_routing.config import RoutingConfig
from payment_routing.database import connect, create_schema
from payment_routing.importers import import_data
from payment_routing.utils import ROOT


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.path = Path(self.directory.name) / "operations.db"
        self.db = create_schema(connect(self.path))
        self.config = RoutingConfig()

    def seed(self, sources=None):
        return import_data(self.db, self.config, sources)

    def snapshot(self):
        return {row[0]: [tuple(r) for r in self.db.execute(f'SELECT * FROM "{row[0]}" ORDER BY rowid')] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    def teardown(self):
        self.db.close()
        self.directory.cleanup()

    def tearDown(self):
        self.teardown()
