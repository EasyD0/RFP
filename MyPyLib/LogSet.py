import logging
from pathlib import Path


class BracketedFormatter(logging.Formatter):
    """
    自定义格式化器：
    将等级名称包装在 [] 中，作为一个整体进行对齐，
    从而保留 [INFO] 的原生结构以支持字体优化。
    """

    def format(self, record):
        # 创建一个新属性，内容为 "[INFO]" 等
        record.level_bracketed = f"[{record.levelname}]"
        return super().format(record)


def logSetUp(
    log_name: str = "",
    log_file: Path | None = None,
    *,
    console_level: int | str = logging.DEBUG,
    file_level: int | str = logging.WARNING,
) -> logging.Logger:
    """
    配置并返回一个包含日志等级的 logging.Logger 对象。
    默认控制台日志等级为 DEBUG, 文件输出等级为 WARNING。
    
    :param log_name: 日志名称标识
    :param log_file: 日志文件路径，为 None 时不输出到文件
    :param console_level: 控制台输出的最低日志等级，支持 int 或字符串(如 "INFO")
    :param file_level: 文件输出的最低日志等级，支持 int 或字符串(如 "DEBUG")
    """
    logger = logging.getLogger(log_name)
    logger.setLevel(logging.DEBUG)

    # 清理旧的 Handler，防止重复打印
    if logger.hasHandlers():
        logger.handlers.clear()

    # --- 格式定义 ---
    # 使用自定义的 level_bracketed 属性，并设置左对齐宽度为 10
    # 这样 [INFO] 后面会补空格，但 [INFO] 内部是紧凑的
    log_format = f"%(asctime)s %(level_bracketed)-10s {log_name} [%(filename)s:%(lineno)d]-%(funcName)s : %(message)s"
    date_format = r"%H:%M:%S (%Y%m%d)"

    formatter = BracketedFormatter(fmt=log_format, datefmt=date_format)

    # 1. 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(console_level)
    logger.addHandler(console_handler)

    # 2. 文件输出（如果指定了路径）
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(file_level)
        logger.addHandler(file_handler)

    return logger


def test():
    logger = logSetUp("MyLogger", Path("./main.log"), console_level="INFO", file_level="DEBUG")
    logger.debug("这是一条调试信息 (DEBUG)")
    logger.info("这是一条普通信息 (INFO)")
    logger.warning("这是一条警告信息 (WARNING)")
    logger.error("这是一条错误信息 (ERROR)")


# --- 测试代码 ---
if __name__ == "__main__":
    test()
