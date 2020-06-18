import pathlib
from pathlib import Path
from typing import List
import subprocess
import shlex
import os

import luigi
import netaddr
from docker.transport import unixconn

from ...lib.base.docker_base_task import DockerBaseTask
from ...lib.base.json_pickle_parameter import JsonPickleParameter
from ...lib.data.container_info import ContainerInfo
from ...lib.data.docker_network_info import DockerNetworkInfo
from ...lib.test_environment.analyze_test_container import DockerTestContainerBuild
from ...lib.test_environment.create_export_directory import CreateExportDirectory
from ...lib.logging.command_log_handler import CommandLogHandler
from ...lib.base.still_running_logger import StillRunningLogger

class SpawnTestContainer(DockerBaseTask):
    environment_name = luigi.Parameter()
    test_container_name = luigi.Parameter()
    network_info = JsonPickleParameter(
        DockerNetworkInfo, significant=False)  # type: DockerNetworkInfo
    ip_address_index_in_subnet = luigi.IntParameter(significant=False)
    attempt = luigi.IntParameter(1)
    reuse_test_container = luigi.BoolParameter(False, significant=False)
    no_test_container_cleanup_after_success = luigi.BoolParameter(False, significant=False)
    no_test_container_cleanup_after_failure = luigi.BoolParameter(False, significant=False)
    use_copy_instead_of_host_mounts = luigi.BoolParameter(False, significant=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.ip_address_index_in_subnet < 0:
            raise Exception(
                "ip_address_index_in_subnet needs to be greater than 0 got %s"
                % self.ip_address_index_in_subnet)

    def register_required(self):
        self.test_container_image_future = self.register_dependency(DockerTestContainerBuild())
        self.export_directory_future = self.register_dependency(CreateExportDirectory())

    def run_task(self):
        subnet = netaddr.IPNetwork(self.network_info.subnet)
        ip_address = str(subnet[2 + self.ip_address_index_in_subnet])
        container_info = None
        if self.network_info.reused and self.reuse_test_container:
            container_info = self._try_to_reuse_test_container(ip_address, self.network_info)
        if container_info is None:
            container_info = self._create_test_container(ip_address, self.network_info)
        test_container = \
            self._client.containers.get(self.test_container_name)
        self._copy_tests()
        self.return_object(container_info)

    def _copy_tests(self):
        self.logger.warning("Copy tests in test container %s.", self.test_container_name)
        test_container = \
            self._client.containers.get(self.test_container_name)
        try:
            test_container.exec_run(cmd="rm -r /tests")
        except:
            pass
        test_container.exec_run(cmd="cp -r /tests_src /tests")

    def _try_to_reuse_test_container(self, ip_address: str,
                                     network_info: DockerNetworkInfo) -> ContainerInfo:
        self.logger.info("Try to reuse test container %s",
                         self.test_container_name)
        container_info = None
        try:
            network_aliases = self._get_network_aliases()
            container_info = self.create_container_info(ip_address, network_aliases, network_info)
        except Exception as e:
            self.logger.warning("Tried to reuse test container %s, but got Exeception %s. "
                                "Fallback to create new database.", self.test_container_name, e)
        return container_info

    def _create_test_container(self, ip_address,
                               network_info: DockerNetworkInfo) -> ContainerInfo:
        self._remove_container(self.test_container_name)
        test_container_image_info = \
            self.get_values_from_futures(self.test_container_image_future)["test-container"]

        # A later task which uses the test_container needs the exported container,
        # but to access exported container from inside the test_container,
        # we need to mount the release directory into the test_container.
        exports_host_path = pathlib.Path(self._get_export_directory()).absolute()
        tests_host_path = pathlib.Path("./tests").absolute()
        volumes = {}
        if not self.use_copy_instead_of_host_mounts:
            volumes = {
                exports_host_path: {
                    "bind": "/exports",
                    "mode": "rw"
                },
                tests_host_path: {
                    "bind": "/tests_src",
                    "mode": "rw"
                }
            }
        docker_unix_sockets = [i for i in self._client.api.adapters.values() if isinstance(i, unixconn.UnixHTTPAdapter)]
        if len(docker_unix_sockets) > 0:
            host_docker_socker_path = docker_unix_sockets[0].socket_path
            volumes[host_docker_socker_path] = {
                "bind": "/var/run/docker.sock",
                "mode": "rw"
            }
        test_container = \
            self._client.containers.create(
                image=test_container_image_info.get_target_complete_name(),
                name=self.test_container_name,
                network_mode=None,
                command="sleep infinity",
                detach=True,
                volumes=volumes,
                labels={"test_environment_name": self.environment_name, "container_type": "test_container"})
        if self.use_copy_instead_of_host_mounts:
            self.logger.warn("Using docker copy instead of host mounts to make tests and exports available")
            tests_tar_log_file = Path(self.get_log_path(),"tests.tar.log")
            tests_tar_file = Path(self.get_output_path(),"tests.tar")
            self._pack_directory(tests_tar_log_file, tests_host_path,tests_tar_file)
            with tests_tar_file.open() as f:
                test_container.put_archive("/tests_src", f)
            os.remove(tests_tar_file)

            exports_tar_log_file = Path(self.get_log_path(),"exports.tar.log")
            exports_tar_file = Path(self.get_output_path(),"exports.tar")
            self._pack_directory(exports_tar_log_file, exports_host_path,exports_tar_file)
            with exports_tar_file.open() as f:
                test_container.put_archive("/export", f)
            os.remove(exports_tar_file)

        docker_network = self._client.networks.get(network_info.network_name)
        network_aliases = self._get_network_aliases()
        docker_network.connect(test_container, ipv4_address=ip_address, aliases=network_aliases)
        test_container.start()
        container_info = self.create_container_info(ip_address, network_aliases, network_info)
        return container_info

    def _pack_directory(self, log_path: Path, directory: str, tar_file: Path):
        content = " ".join("'%s'" % file for file in os.listdir(directory))
        command = f"""tar -C '{directory}' -cf '{tar_file}' {content}"""
        tar_file.parent.mkdir(parents=True,exist_ok=True)
        self.run_command(command, "packing tests directory", log_path)

    def run_command(self, command: str, description: str, log_file_path: Path):
        with subprocess.Popen(shlex.split(command), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as process:
            with CommandLogHandler(log_file_path, self.logger, description) as log_handler:
                still_running_logger = StillRunningLogger(
                    self.logger, description)
                log_handler.handle_log_lines((command + "\n").encode("utf-8"))
                for line in iter(process.stdout.readline, b''):
                    still_running_logger.log()
                    log_handler.handle_log_lines(line)
                process.wait(timeout=60 * 2)
                return_code_log_line = "return code %s" % process.returncode
                log_handler.handle_log_lines(return_code_log_line.encode("utf-8"), process.returncode != 0)

    def _get_network_aliases(self):
        network_aliases = ["test_container", self.test_container_name]
        return network_aliases

    def create_container_info(self, ip_address: str, network_aliases: List[str],
                              network_info: DockerNetworkInfo) -> ContainerInfo:
        test_container = self._client.containers.get(self.test_container_name)
        if test_container.status != "running":
            raise Exception(f"Container {self.test_container_name} not running")
        container_info = ContainerInfo(container_name=self.test_container_name,
                                       ip_address=ip_address,
                                       network_aliases=network_aliases,
                                       network_info=network_info)
        return container_info

    def _get_export_directory(self):
        return self.get_values_from_future(self.export_directory_future)

    def _remove_container(self, container_name: str):
        try:
            container = self._client.containers.get(container_name)
            container.remove(force=True)
            self.logger.info("Removed container %s", container_name)
        except Exception as e:
            pass

    def cleanup_task(self, success):
        if (success and not self.no_test_container_cleanup_after_success) or \
            (not success and not self.no_test_container_cleanup_after_failure):
            try:
                self.logger.info(f"Cleaning up container %s",self.test_container_name)
                self._remove_container(self.test_container_name)
            except Exception as e:
                self.logger.error(f"Error during removing container %s: %s",self.test_container_name,e)

