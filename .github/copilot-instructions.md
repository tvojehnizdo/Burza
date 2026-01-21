# Copilot Instructions for Burza

## About This Repository

Burza is a project repository. This file provides guidance to GitHub Copilot when assisting with code development.

## General Guidelines

- Follow consistent coding standards throughout the codebase
- Write clear, maintainable code with appropriate comments when necessary
- Ensure all changes are properly tested before committing
- Keep commits atomic and focused on single concerns

## Code Style

- Use meaningful variable and function names
- Follow the existing code structure and patterns in the repository
- Keep functions small and focused on a single responsibility
- Follow language-specific style guides (e.g., PEP 8 for Python, PSR-12 for PHP, Airbnb style for JavaScript)
- Use proper indentation (2 or 4 spaces depending on language convention)
- Avoid code duplication - extract common logic into reusable functions or modules

## Code Quality & Development

- **Static Analysis**: Run linters and static analysis tools before committing
  - Use ESLint for JavaScript/TypeScript
  - Use Pylint/Flake8 for Python
  - Use PHPStan/Psalm for PHP
- **Type Safety**: Use type hints and strict typing where available
- **Error Handling**: Implement proper error handling with informative messages
- **Security**: Follow OWASP guidelines and scan for vulnerabilities
- **Performance**: Consider performance implications, especially in loops and database queries
- **Code Reviews**: All changes should be reviewed before merging

## Testing

- Write tests for new functionality
- Ensure existing tests pass before committing changes
- Follow the testing patterns established in the repository
- **Test Coverage**: Aim for meaningful test coverage (unit, integration, and end-to-end)
- **TDD Approach**: Consider writing tests before implementation
- Use appropriate testing frameworks for your language

## Documentation

- Update documentation when adding or modifying features
- Keep the README.md up to date with accurate information
- Document complex logic with inline comments
- **API Documentation**: Document all public APIs and interfaces
- **Changelog**: Maintain a changelog for significant changes
- **Architecture Decisions**: Document important architectural decisions

## CI/CD & Deployment

### Continuous Integration
- Set up GitHub Actions workflows for automated testing
- Run tests on every pull request
- Use automated linting and code quality checks
- Configure branch protection rules

### Deployment
- **Environment Setup**: Maintain separate development, staging, and production environments
- **Automated Deployment**: Use CI/CD pipelines for automated deployments
- **Environment Variables**: Use environment variables for configuration (never commit secrets)
- **Docker**: Consider containerization for consistent deployments
- **Versioning**: Use semantic versioning (MAJOR.MINOR.PATCH)
- **Rollback Strategy**: Always have a rollback plan for deployments

### Deployment Best Practices
- Test deployments in staging before production
- Use feature flags for gradual rollouts
- Monitor application health after deployments
- Keep deployment scripts in version control
- Document deployment procedures

## Version Control

- Use meaningful commit messages following conventional commits format
  - `feat:` for new features
  - `fix:` for bug fixes
  - `docs:` for documentation changes
  - `refactor:` for code refactoring
  - `test:` for adding tests
  - `chore:` for maintenance tasks
- Create feature branches from main/master
- Keep pull requests focused and small
- Squash commits when merging to keep history clean
- Never commit sensitive data (API keys, passwords, credentials)

## Best Practices

- Review changes carefully before committing
- Use version control best practices (atomic commits, descriptive messages)
- Consider edge cases and error handling
- Optimize for readability and maintainability over cleverness
- Keep dependencies up to date and audit for vulnerabilities
- Use `.gitignore` to exclude build artifacts and dependencies
- Write code that is easy to debug and troubleshoot
