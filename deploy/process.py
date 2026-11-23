"""Manage only the backend registered to the selected installation."""
from deploy.config import DeployConfig, ExecutionError
from deploy.instance import InstanceError, prepare_instance
from deploy.logger import logger


class ProcessManager(DeployConfig):
    def process_kill(self):
        logger.hr("Prepare this OAS installation", 0)
        try:
            prepare_instance(self.root_filepath)
        except (InstanceError, OSError) as exc:
            logger.error(str(exc))
            raise ExecutionError(str(exc)) from exc
