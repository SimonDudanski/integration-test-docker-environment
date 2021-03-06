import luigi

from .....lib.base.docker_base_task import DockerBaseTask
from .....lib.base.json_pickle_parameter import JsonPickleParameter
from .....lib.docker.images.image_info import ImageInfo


class DockerImageCreatorBaseTask(DockerBaseTask):
    image_name = luigi.Parameter()
    # ParameterVisibility needs to be hidden instead of private, because otherwise a MissingParameter gets thrown
    image_info = JsonPickleParameter(ImageInfo,
                                     visibility=luigi.parameter.ParameterVisibility.HIDDEN,
                                     significant=True)  # type:ImageInfo
