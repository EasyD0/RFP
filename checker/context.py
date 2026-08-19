"""
checker.context - 编译上下文 (CodeContext).

负责: 初始化 Preprocessor, 生成 compile_commands.json, 提供编译参数查询
和文件归属判断. 对象整体可序列化, 供多进程 worker 反序列化复用.
"""

import json
from pathlib import Path

from MyPyLib.Preprocessor import Preprocessor


# TODO 若问题被clear_false() 它接下来不应该继续执行检查了
#  problem的排序函数 可能需要重新编写
class CodeContext:
    """
    代码处理工具

    注意: 对象整体可序列化 (Preprocessor / all_used_files 等字段均可
    pickle). multiprocessing 时直接序列化整个实例传给 worker, worker
    通过反序列化恢复对象, 不会重新执行 __init__, 因此不会在子进程中
    重复初始化 Preprocessor 或重写 .resp / compile_commands.json.
    """

    def __init__(
        self,
        proj_dir: Path,
        proj_name: str = "",
        chip_name: str = "",
        compile_dir: Path = Path("./clangd/"),
    ):
        self.proj_dir = proj_dir.resolve()
        self.compile_dir = compile_dir.resolve()
        self.proj_name = proj_name
        self.chip_name = chip_name

        self.por: Preprocessor = Preprocessor(
            self.proj_dir,
            response_dir=Path("./.resp"),
            proj_name=proj_name,
            chip_name=chip_name,
        )
        # 绝对路径集合
        self.all_used_files = self.por.getUsedFiles()
        self.__post_init__()

    def __post_init__(self):
        self.compile_dir = self.generate_command_json(self.compile_dir)

    def get_args(self, file: Path):
        """
        路径可以是相对/绝对路径
        :param file:
        :return:
        """
        return self.por.get_args(file)

    def generate_command_json(self, save_dir: Path = Path("./clangd/")):
        """
        为C语言项目生成 compile_commands.json 文件, 供clangd使用, save_dir是存放 compile_commands.json文件的位置
        """

        def get_json_data(
            source_file: Path, macro_list: list[str], inc_list: list[str]
        ) -> dict:
            """
            为一个文件条目生成在 compile_commands.json中的内容
            source_file: 相对路径
            macro_list: 形如 ["-D macro1", "-D macro2=1"]
            inc_list: 形如 ["-ID:/dir/",]
            """
            # source_file 可能是相对路径(相对 proj_dir), 也可能是绝对路径, 统一转为绝对路径
            src = source_file
            if not src.is_absolute():
                src = self.proj_dir / src
            src = src.resolve()

            arguments: list[str] = ["clang"]

            # include 参数, 形如 "-ID:/dir/" 或 "-I D:/dir/", 每个元素作为一个整体加入
            for inc_arg in inc_list:
                inc_arg = str(inc_arg).strip()
                if not inc_arg or inc_arg.startswith("#"):
                    continue
                if inc_arg.startswith("-I ") or inc_arg.startswith("-D "):
                    # "-I path" / "-D macro" 拆成两个参数, clang 接受该形式
                    flag, _, value = inc_arg.partition(" ")
                    if value.strip():
                        arguments.append(flag)
                        arguments.append(value.strip())
                else:
                    arguments.append(inc_arg)

            # 宏定义参数, 形如 "-D macro1" / "-Dmacro2=1"
            for macro_arg in macro_list:
                macro_arg = str(macro_arg).strip()
                if not macro_arg:
                    continue
                if macro_arg.startswith("-D "):
                    macro_name = macro_arg[3:].strip()
                    if macro_name:
                        arguments.append("-D")
                        arguments.append(macro_name)
                else:
                    arguments.append(macro_arg)

            return {
                "directory": self.proj_dir.as_posix(),
                "file": src.as_posix(),
                "arguments": arguments,
            }

        json_data: list[dict] = []
        resp_file_content: dict[Path, list] = {}
        for file in self.all_used_files:
            _, macro, inc = self.por.core_macro_inc(file)
            if not inc in resp_file_content:
                with open(
                    inc,
                    "r",
                    encoding="utf-8",
                ) as f:
                    _content = f.readlines()
                resp_file_content[inc] = _content

            inc_content = resp_file_content[inc]
            json_data.append(get_json_data(file, macro, inc_content))

        save_dir.mkdir(parents=True, exist_ok=True)
        json_file = save_dir / "compile_commands.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=4)

        return json_file

    def is_used_file(self, file: Path):
        """文件路径可以是绝对路径/相对工程根的路径"""
        if not file.is_absolute():
            abs_path = self.proj_dir.absolute() / file
        else:
            abs_path = file

        if not abs_path.exists():
            return False

        return abs_path in self.all_used_files or file in self.all_used_files
