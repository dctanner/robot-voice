import pytest

from robot_voice.config import Config
from robot_voice.memory.store import Store
from robot_voice.robot.mock import MockRobot
from robot_voice.robot.tasks import TaskRunner
from robot_voice.tools.gateway import Gateway


@pytest.fixture
def config(tmp_path):
    return Config(database=tmp_path / "test.sqlite3", timezone="UTC", mock_task_seconds=0.04)


@pytest.fixture
def store(config):
    value = Store(config.database)
    yield value
    value.close()


@pytest.fixture
def rig(config, store):
    session = store.session(config)
    runner = TaskRunner(store, MockRobot(0.04))
    return session, runner, Gateway(store, runner)
