import time
from datetime import datetime, timedelta

import luigi
from docker.models.containers import Container

from ...lib.base.docker_base_task import DockerBaseTask
from ...lib.base.json_pickle_parameter import JsonPickleParameter
from ...lib.data.container_info import ContainerInfo
from ...lib.data.database_credentials import DatabaseCredentialsParameter
from ...lib.data.database_info import DatabaseInfo
from ...lib.test_environment.is_database_ready_thread import IsDatabaseReadyThread


class WaitForTestExternalDatabase(DockerBaseTask,
                                  DatabaseCredentialsParameter):
    environment_name = luigi.Parameter()
    test_container_info = JsonPickleParameter(ContainerInfo, significant=False)  # type: ContainerInfo
    database_info = JsonPickleParameter(DatabaseInfo, significant=False)  # type: DatabaseInfo
    db_startup_timeout_in_seconds = luigi.IntParameter(1 * 60, significant=False)
    attempt = luigi.IntParameter(1)

    def run_task(self):
        test_container = self._client.containers.get(self.test_container_info.container_name)
        is_database_ready = self.wait_for_database_startup(test_container)
        self.return_object(is_database_ready)

    def wait_for_database_startup(self, test_container: Container):
        is_database_ready_thread = self.start_wait_threads(test_container)
        is_database_ready = self.wait_for_threads(is_database_ready_thread)
        self.join_threads(is_database_ready_thread)
        return is_database_ready

    def start_wait_threads(self, test_container):
        is_database_ready_thread = IsDatabaseReadyThread(self.logger,
                                                         self.database_info,
                                                         self.get_database_credentials(),
                                                         test_container)
        is_database_ready_thread.start()
        return is_database_ready_thread

    def join_threads(self, is_database_ready_thread: IsDatabaseReadyThread):
        is_database_ready_thread.stop()
        is_database_ready_thread.join()

    def wait_for_threads(self, is_database_ready_thread: IsDatabaseReadyThread):
        is_database_ready = False
        reason = None
        start_time = datetime.now()
        while (True):
            if is_database_ready_thread.finish:
                is_database_ready = True
                break
            if self.timeout_occured(start_time):
                reason = f"timeout after after {self.db_startup_timeout_in_seconds} seconds"
                is_database_ready = False
                break
            time.sleep(1)
        if not is_database_ready:
            self.log_database_not_ready(is_database_ready_thread, reason)
        is_database_ready_thread.stop()
        return is_database_ready

    def log_database_not_ready(self, is_database_ready_thread, reason):
        log_information = f"""
========== IsDatabaseReadyThread output db connection: ============
{is_database_ready_thread.output_db_connection}
========== IsDatabaseReadyThread output bucketfs connection: ============
{is_database_ready_thread.output_bucketfs_connection}
"""
        self.logger.warning(
            'Database startup failed for following reason "%s", here some debug information \n%s',
            reason, log_information)

    def timeout_occured(self, start_time):
        timeout = timedelta(seconds=self.db_startup_timeout_in_seconds)
        return datetime.now() - start_time > timeout
