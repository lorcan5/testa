# main.py

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Form
from pydantic import BaseModel, Field
import os
import shutil
import asyncio
from typing import Optional
import uuid  # 导入 uuid 模块用于生成唯一文件名
import time  # 用于更精确的日志时间戳，如果需要

# 导入你的打印机 API 类和日志器
from ats_real_printer_api import AtsRealPrinterApi, logger

# --- 配置 ---
UPLOAD_DIRECTORY = "temp_uploads"  # 用于临时存储上传文件的目录

# 确保上传目录存在
os.makedirs(UPLOAD_DIRECTORY, exist_ok=True)

app = FastAPI(
    title="ATS 动态打印机 API",
    description="一个 RESTful API，允许动态指定打印机连接信息与 ATS 真实打印机交互。",
    version="1.0.0"
)


# --- Pydantic 模型 ---
class PrinterConnectionInfo(BaseModel):
    host: str
    port: int


class SetVarRequest(BaseModel):
    connection: PrinterConnectionInfo
    setting_name: str
    setting_value: str


class GetVarRequest(BaseModel):
    connection: PrinterConnectionInfo
    setting_name: str
    timeout_seconds: int = Field(5, description="等待响应的最大时间（秒）", ge=1, le=60)  # 默认5秒，范围限制


class GetVarResponse(BaseModel):
    setting_name: str
    value: str


class MessageResponse(BaseModel):
    message: str


# 为 send_and_wait_for_response 添加的模型
class SendAndWaitRequest(BaseModel):
    connection: PrinterConnectionInfo
    data: str
    bufsize: int = Field(8192, description="读取响应的缓冲区大小（字节）", ge=128)
    milis: int = Field(1000, description="等待响应的时间（毫秒）", ge=100, le=60000)


class SendAndWaitResponse(BaseModel):
    sent_data: str
    received_response: str


# 为新增的 send_command 端点添加的模型
class SendCommandRequest(BaseModel):
    connection: PrinterConnectionInfo
    data: str = Field(..., description="要发送给打印机的原始字符串数据（例如打印机命令或查询）。")


# --- 辅助函数：动态获取打印机 API 实例 ---
# 这是一个依赖项工厂函数，每次请求都会创建一个新的连接
async def get_dynamic_printer_api_instance(host: str, port: int) -> AtsRealPrinterApi:
    """
    提供一个连接到指定 host:port 的 AtsRealPrinterApi 实例。
    每次请求都会尝试建立新的连接并在处理后关闭。
    """
    printer_api = AtsRealPrinterApi(host, port)
    try:
        await printer_api.connect()
        yield printer_api  # 'yield' 使得这个函数成为一个生成器依赖，在请求处理后会执行 finally 块
    except IOError as e:
        logger.error(f"Dependency error: Unable to connect to printer {host}:{port}: {e}")
        raise HTTPException(
            status_code=503,  # Service Unavailable
            detail=f"无法连接到打印机 {host}:{port}。请检查打印机状态和提供的连接信息。"
        )
    finally:
        await printer_api.close()  # 确保在请求处理完毕后关闭连接


# --- API 端点 ---

# 1. 设置打印机变量 (修正：不等待响应)
@app.post("/printer/set_variable", response_model=MessageResponse, summary="设置打印机变量")
async def set_printer_variable(request: SetVarRequest):
    """
    连接到指定打印机并设置一个命名变量的值。
    - `connection`: 包含 `host` 和 `port` 的连接信息。
    - `setting_name`: 要设置的变量名称（例如 "device.name", "printer.label_count"）。
    - `setting_value`: 变量的新值。
    此操作通常是“发送即忘”，不期望打印机返回响应。
    """
    start_time = time.time()
    async for printer_api in get_dynamic_printer_api_instance(request.connection.host, request.connection.port):
        try:
            await printer_api.set_variable(request.setting_name, request.setting_value)
            end_time = time.time()
            logger.info(
                f"成功设置打印机 {request.connection.host}:{request.connection.port} 变量 {request.setting_name} 为 {request.setting_value}. 耗时: {end_time - start_time:.4f}s")
            return {"message": f"变量 '{request.setting_name}' 已成功设置为 '{request.setting_value}'。"}
        except IOError as e:
            logger.error(f"设置打印机 {request.connection.host}:{request.connection.port} 变量时出错: {e}")
            raise HTTPException(status_code=500, detail="设置变量失败，请检查打印机状态或提供的参数。")


# 2. 获取打印机变量
@app.post("/printer/get_variable", response_model=GetVarResponse, summary="获取打印机变量")
async def get_printer_variable(request: GetVarRequest):
    """
    连接到指定打印机并获取一个命名变量的值。
    - `connection`: 包含 `host` 和 `port` 的连接信息。
    - `setting_name`: 要获取的变量名称（例如 "device.name", "device.uptime"）。
    - `timeout_seconds`: 可选。等待打印机响应的最大时间（秒）。
    此操作会等待打印机返回变量值。
    """
    start_time = time.time()
    async for printer_api in get_dynamic_printer_api_instance(request.connection.host, request.connection.port):
        try:
            # 将秒转换为毫秒传递给 get_variable 方法
            value = await printer_api.get_variable(request.setting_name, request.timeout_seconds * 1000)
            end_time = time.time()
            logger.info(
                f"成功获取打印机 {request.connection.host}:{request.connection.port} 变量 {request.setting_name}: {value}. 耗时: {end_time - start_time:.4f}s")
            return {"setting_name": request.setting_name, "value": value}
        except IOError as e:
            logger.error(f"获取打印机 {request.connection.host}:{request.connection.port} 变量时出错: {e}")
            raise HTTPException(status_code=500, detail="获取变量失败，请检查打印机状态或提供的参数。")
        except asyncio.TimeoutError as e:
            logger.error(f"获取打印机 {request.connection.host}:{request.connection.port} 变量超时: {e}")
            raise HTTPException(status_code=504, detail="获取变量超时，打印机未在预期时间内响应。")


# 3. 发送文件到打印机
@app.post("/printer/send_file", response_model=MessageResponse, summary="发送文件到打印机")
async def send_file_to_printer(
        file: UploadFile = File(..., description="要发送到打印机的打印命令或数据文件。"),
        host: str = Form(..., description="打印机的 IP 地址或主机名。"),
        port: int = Form(..., description="打印机端口，通常是 9100。")
):
    """
    将上传的文件内容直接发送到指定打印机。
    这适用于发送包含 ZPL、EPL 或其他打印机命令的文本文件。
    """
    start_time = time.time()
    # 使用 uuid 生成唯一文件名，提高安全性并避免命名冲突
    unique_filename = f"{uuid.uuid4().hex}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIRECTORY, unique_filename)

    try:
        # 将上传文件保存到临时目录
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.info(f"临时文件 '{file_path}' 创建成功，大小: {os.path.getsize(file_path)} bytes.")

        # 创建打印机 API 实例并发送文件
        async for printer_api in get_dynamic_printer_api_instance(host, port):
            await printer_api.send_file_to_printer(file_path)
            end_time = time.time()
            logger.info(f"文件 '{file.filename}' 成功发送到打印机 {host}:{port}. 耗时: {end_time - start_time:.4f}s")
            return {"message": f"文件 '{file.filename}' 已成功发送到打印机 {host}:{port}。"}

    except IOError as e:
        logger.error(f"发送文件 '{file.filename}' 到 {host}:{port} 时出错: {e}")
        raise HTTPException(status_code=500, detail="发送文件失败，请检查打印机状态或文件内容。")
    except ValueError as e:
        logger.error(f"文件处理错误: {e}")
        raise HTTPException(status_code=400, detail=f"文件处理错误: {e}")
    finally:
        # 清理临时文件
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"临时文件 '{file_path}' 已清理。")


# 4. 发送数据并等待响应 (适用于需要打印机反馈的场景)
@app.post("/printer/send_and_wait_for_response", response_model=SendAndWaitResponse,
          summary="向打印机发送原始数据并等待响应")
async def send_data_and_wait_for_response(request: SendAndWaitRequest):
    """
    向指定打印机发送原始数据，并等待从打印机返回的响应。
    这适用于需要发送自定义命令并接收直接反馈的场景。

    - `connection`: 包含 `host` 和 `port` 的连接信息。
    - `data`: 要发送给打印机的原始字符串数据（例如打印机命令或查询）。
    - `bufsize`: 可选。从打印机读取响应的缓冲区大小（字节）。默认8192。
    - `milis`: 可选。等待打印机响应的时间（毫秒）。默认1000（1秒）。
    """
    start_time = time.time()
    async for printer_api in get_dynamic_printer_api_instance(request.connection.host, request.connection.port):
        try:
            response_data = await printer_api.send_and_wait_for_response(
                request.data,
                request.bufsize,
                request.milis
            )
            end_time = time.time()
            logger.info(
                f"发送数据到 {request.connection.host}:{request.connection.port} 成功。接收到响应: {response_data.strip()[:100]}.... 耗时: {end_time - start_time:.4f}s")
            return {
                "sent_data": request.data,
                "received_response": response_data
            }
        except IOError as e:
            logger.error(f"向 {request.connection.host}:{request.connection.port} 发送数据并等待响应时出错: {e}")
            raise HTTPException(status_code=500, detail="发送数据并等待响应失败，请检查打印机状态或命令。")
        except asyncio.TimeoutError as e:
            logger.error(f"向 {request.connection.host}:{request.connection.port} 发送数据并等待响应超时: {e}")
            raise HTTPException(status_code=504, detail="发送数据并等待响应超时，打印机未在预期时间内响应。")


# 5. 新增：向打印机发送原始命令（不等待响应）
@app.post("/printer/send_command", response_model=MessageResponse, summary="向打印机发送原始命令（不等待响应）")
async def send_printer_command(request: SendCommandRequest):
    """
    向指定打印机发送原始命令或数据，不等待任何响应。
    适用于“发送即忘”的命令，例如设置变量，或发送打印任务。
    """
    start_time = time.time()
    async for printer_api in get_dynamic_printer_api_instance(request.connection.host, request.connection.port):
        try:
            await printer_api.write_data(request.data)
            end_time = time.time()
            logger.info(
                f"成功发送命令到 {request.connection.host}:{request.connection.port}: {request.data.strip()[:100]}.... 耗时: {end_time - start_time:.4f}s")
            return {"message": "命令已成功发送到打印机。"}
        except IOError as e:
            logger.error(f"发送命令到 {request.connection.host}:{request.connection.port} 时出错: {e}")
            raise HTTPException(status_code=500, detail="发送命令失败，请检查打印机状态或命令。")


@app.get("/", include_in_schema=False)  # 不显示在文档中
async def read_root():
    return {"message": "欢迎使用 ATS 动态打印机 API！请访问 /docs 获取 API 文档。"}
