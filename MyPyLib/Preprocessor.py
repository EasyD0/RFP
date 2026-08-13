# 预处理代码的类, 隐藏实现的细节
from pathlib import Path


class Preprocessor_impl:
    def get_args(self, source_file: Path)->list[str]:
        """
        获取预处理的参数
        :param source_file:
        :return:
        """
        pass

    def getUsedFiles(self)->set[Path]:
        pass

class Preprocessor:
    def __init__(
            self,
            proj_dir:Path,
            response_dir:Path,
            proj_name:str | None = None,
            chip_name:str | None = None,
        ):
            self.por:Preprocessor_impl = Preprocessor_impl()

    def get_args(self, source_file: Path)->list[str]:
        return self.por.get_args(source_file)

    def getUsedFiles(self)->set[Path]:
        return self.por.getUsedFiles()