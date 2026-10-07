"""Swagger authentication contract for the Project Sources operator path."""

from fastapi import FastAPI


def test_project_source_openapi_uses_standard_bearer_security_scheme():
    """Swagger must expose a usable Bearer Authorize control for CT-05."""
    from app.api.v1.projects import router as projects_router

    app = FastAPI()
    app.include_router(projects_router, prefix="/api/projects")
    schema = app.openapi()
    operation = schema["paths"]["/api/projects/{project_id}/sources"]["get"]

    security = operation["security"]
    scheme_name = next(iter(security[0]))
    scheme = schema["components"]["securitySchemes"][scheme_name]

    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert not any(
        parameter["name"] == "authorization" and parameter["in"] == "header"
        for parameter in operation.get("parameters", [])
    )
