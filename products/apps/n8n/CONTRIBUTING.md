# Contributing to Woow n8n MCP Server

Thank you for your interest in contributing! This guide will help you get started.

## Development Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/WOOWTECH/woow_n8n_mcp_server.git
   cd woow_n8n_mcp_server
   ```

2. **Install Python dependencies**

   ```bash
   pip install -e ".[dev]"
   pip install -e "./n8n_mcp_admin[dev]"
   ```

3. **Install frontend dependencies**

   ```bash
   cd frontend && npm install
   ```

4. **Start development servers**

   ```bash
   # Backend (FastAPI with auto-reload)
   uvicorn n8n_mcp_admin.main:app --reload --port 8080

   # Frontend (Vite dev server with proxy)
   cd frontend && npm run dev
   ```

## Code Style

- **Python:** Follow PEP 8. Use type hints. All new code must include docstrings.
- **JavaScript/React:** Use functional components with hooks. Follow the existing project patterns.
- **Commits:** Write clear, descriptive commit messages. Reference issue numbers when applicable.

## Pull Request Process

1. Fork the repository and create a feature branch from `main`.
2. Write or update tests for your changes.
3. Ensure all tests pass: `pytest` for backend, `npm test` for frontend.
4. Update documentation if your change affects the public API or user-facing features.
5. Submit a pull request with a clear description of your changes.

## Reporting Issues

- Use [GitHub Issues](https://github.com/WOOWTECH/woow_n8n_mcp_server/issues) for bug reports and feature requests.
- Include steps to reproduce, expected behavior, and actual behavior for bug reports.
- Attach screenshots or logs when applicable.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
