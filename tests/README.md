# Telegram 视频下载器 - 测试

本目录包含项目的单元测试和集成测试。

## 运行测试

### 安装测试依赖

```bash
pip install -r requirements-dev.txt
```

### 运行所有测试

```bash
pytest
```

### 运行特定测试文件

```bash
pytest tests/test_relay_tokens.py
```

### 查看测试覆盖率

```bash
pytest --cov=. --cov-report=html
```

覆盖率报告将生成在 `htmlcov/` 目录。

### 详细输出

```bash
pytest -v
```

### 仅运行失败的测试

```bash
pytest --lf
```

## 测试结构

- `conftest.py`: 共享的 fixtures 和测试配置
- `test_relay_tokens.py`: relay token 生成和验证测试
- `test_aria2_client.py`: legacy aria2 客户端单元测试，仅覆盖旧入口兼容模块

## Mock 数据

测试使用 `unittest.mock` 和 `pytest-mock` 来模拟外部依赖：

- Telegram API 调用
- legacy aria2 RPC 调用
- 文件系统操作
- 网络请求

## 注意事项

- 所有测试都是隔离的，不会影响实际的 Telegram 连接或文件系统
- 临时文件会在测试后自动清理
- 使用 Mock 对象避免真实的外部 API 调用
