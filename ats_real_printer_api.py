import socket
import logging
import time
import asyncio
from typing import Optional
from pathlib import Path
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class AtsRealPrinterApi:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def connect(self):
        """
        连接到打印机。
        会尝试多次连接，如果失败则抛出 IOError。
        """
        max_retries = 2
        delay_seconds = 2

        for attempt in range(max_retries + 1):
            try:
                # 使用 asyncio.open_connection 进行异步套接字操作
                # timeout 参数用于连接尝试本身
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                logger.info(f"Connected to printer at {self.host}:{self.port}")
                return
            except Exception as e:
                logger.error(f"Attempt {attempt + 1}: Failed to connect to printer at {self.host}:{self.port}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(delay_seconds)
                else:
                    raise IOError(
                        f"Failed to connect to printer at {self.host}:{self.port} after {max_retries + 1} attempts.") from e

    async def write_data(self, data: str):
        """
        发送数据到打印机。
        """
        if self.writer and not self.writer.is_closing():
            try:
                self.writer.write(data.encode('utf-8'))
                await self.writer.drain()  # 确保数据被发送出去
                logger.debug(f"Successfully wrote data to printer: {data.strip()[:50]}...")
            except Exception as e:
                logger.error(f"Error writing data to {self.host}:{self.port}: {e}")
                raise IOError(f"Failed to write data to printer: {e}")
        else:
            logger.error(f"Printer {self.host}:{self.port} is not connected or writer is closed for write_data.")
            raise IOError("Printer is not connected")

    async def read_data(self, bufsize: int, timeout_milis: Optional[int] = None) -> str:
        """
        从打印机读取数据。
        如果指定了 timeout_milis，则在超时后抛出 asyncio.TimeoutError。
        """
        if self.reader and not self.reader.at_eof():
            try:
                if timeout_milis is not None:
                    data = await asyncio.wait_for(self.reader.read(bufsize), timeout=timeout_milis / 1000.0)
                else:
                    data = await self.reader.read(bufsize)

                if not data:
                    logger.warning(
                        f"No data received from printer {self.host}:{self.port} within bufsize {bufsize} after waiting.")
                    return ""  # 返回空字符串表示没有读取到数据

                decoded_data = data.decode('utf-8', errors='ignore')  # 忽略解码错误
                logger.debug(f"Successfully read data from printer: {decoded_data.strip()[:50]}...")
                return decoded_data
            except asyncio.TimeoutError:
                logger.error(
                    f"Timeout occurred while reading data from {self.host}:{self.port} after {timeout_milis}ms.")
                raise IOError(f"Timeout while reading data from printer after {timeout_milis}ms.")
            except Exception as e:
                logger.error(f"Error reading data from {self.host}:{self.port}: {e}")
                raise IOError(f"Failed to read data from printer: {e}")
        else:
            logger.error(f"Printer {self.host}:{self.port} is not connected or reader is closed for read_data.")
            raise IOError("Printer is not connected")

    async def send_and_wait_for_response(self, data: str, bufsize: int, milis: int = 1000) -> str:
        """
        发送数据到打印机并等待响应。
        在发送后等待指定毫秒，然后尝试读取数据。
        """
        logger.info(
            f"[{self.host}:{self.port}] send_and_wait_for_response: Sending '{data.strip()[:50]}...' and waiting for {milis}ms for response.")
        await self.write_data(data)

        # 在尝试读取之前，强制等待一段时间，给打印机处理和响应的时间
        await asyncio.sleep(milis / 1000.0)

        # 读取数据时，传入超时参数，防止无限等待
        return await self.read_data(bufsize, timeout_milis=milis)

    async def set_variable(self, setting_name: str, setting_value: str):
        """
        在打印机上设置一个变量。
        注意：对于 setvar 命令，通常打印机不会立即返回响应，因此这里只发送数据，不等待读取。
        """
        command = f'! U1 setvar "{setting_name}" "{setting_value}"\r\n'
        logger.info(f"[{self.host}:{self.port}] Setting variable: {setting_name} to {setting_value}")
        await self.write_data(command)
        # 不调用 read_data 或 send_and_wait_for_response，因为 setvar 通常是 fire-and-forget

    async def get_variable(self, setting_name: str, timeout_milis: int = 5000) -> str:
        """
        从打印机获取一个变量的值。
        会发送查询命令并等待响应。
        """
        command = f'! U1 getvar "{setting_name}"\r\n'
        logger.info(f"[{self.host}:{self.port}] Getting variable: {setting_name} with timeout {timeout_milis}ms.")

        # 对于 getvar，我们期望有响应，所以使用 send_and_wait_for_response
        # 提供一个较大的缓冲区，因为变量内容可能较长
        response = (await self.send_and_wait_for_response(command, 8192, timeout_milis)).strip()
        logger.info(f"[{self.host}:{self.port}] Received response for {setting_name}: {response.strip()[:100]}...")

        # 移除可能的引号和多余的空白字符
        return response.replace('"', '').strip()

    async def send_file_to_printer(self, file_path: str):
        """
        将文件内容传输到打印机。
        """
        if not self.writer or self.writer.is_closing():
            raise IOError("Printer is not connected")

        file_path_obj = Path(file_path)
        if not file_path_obj.exists() or not file_path_obj.is_file():
            raise ValueError(f"Invalid file path: {file_path}")

        try:
            logger.info(f"[{self.host}:{self.port}] Starting file transfer from {file_path} to printer...")
            with open(file_path_obj, 'rb') as f:
                while True:
                    chunk = f.read(1024)  # Read in chunks of 1KB
                    if not chunk:
                        break
                    self.writer.write(chunk)
                    await self.writer.drain()
                    # logger.debug(f"Sent {len(chunk)} bytes from file.") # 避免过多日志
            logger.info(f"[{self.host}:{self.port}] File transfer completed for {file_path}.")
        except Exception as e:
            logger.error(f"[{self.host}:{self.port}] An error occurred during file transfer of {file_path}: {e}")
            raise IOError(f"An error occurred during file transfer of {file_path}: {e}") from e

    async def close(self):
        """
        关闭与打印机的连接。
        """
        if self.writer and not self.writer.is_closing():
            try:
                self.writer.close()
                await self.writer.wait_closed()
                self.reader = None
                self.writer = None
                logger.info(f"Disconnected from printer {self.host}:{self.port}")
            except Exception as e:
                logger.error(f"Error closing connection to {self.host}:{self.port}: {e}")
        else:
            logger.warning(f"Printer {self.host}:{self.port} is not connected or already closed.")

    # 如果需要，可以添加 waitUntilPrinterIsDisconnected 和 waitUntilPrinterIsConnected 方法
    # 但它们通常涉及主动ping或监听事件，此处为了保持核心功能简洁，暂不实现。
