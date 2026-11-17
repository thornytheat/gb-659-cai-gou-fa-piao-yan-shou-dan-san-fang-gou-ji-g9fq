from fastapi import Request

def page(request: Request, template: str, title: str, endpoint: str, rows: list[dict], context: dict | None = None):
    payload = {"title": title, "endpoint": endpoint, "rows": rows, "version": request.app.state.version}
    if context:
        payload.update(context)
    return request.app.state.templates.TemplateResponse(request, template, payload)
