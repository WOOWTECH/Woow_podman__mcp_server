# Contributing to Woow Odoo MCP Server

Thank you for your interest in contributing! This guide will help you get started.

## Development Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/WOOWTECH/woow_odoo_mcp_server.git
   cd woow_odoo_mcp_server
   ```

2. **Install Python dependencies**

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. **Install frontend dependencies**

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

4. **Run the backend**

   ```bash
   uvicorn odoo_mcp_admin.main:app --host 0.0.0.0 --port 8080 --reload
   ```

## Code Style

- **Python**: We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. Run `ruff check .` and `ruff format .` before committing.
- **JavaScript/React**: Follow the existing code style. Use functional components with hooks.

## Pull Request Process

1. Fork the repository and create a feature branch from `main`.
2. Write clear, descriptive commit messages.
3. Ensure all existing tests pass.
4. Add tests for new functionality.
5. Update documentation if you change any API endpoints or configuration options.
6. Submit a pull request with a clear description of your changes.

## Reporting Issues

- Use [GitHub Issues](https://github.com/WOOWTECH/woow_odoo_mcp_server/issues) to report bugs or request features.
- Include steps to reproduce the issue, expected behavior, and actual behavior.
- Attach relevant log output or screenshots if applicable.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
