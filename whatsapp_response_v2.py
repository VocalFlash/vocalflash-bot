def build_whatsapp_response(api_data, legacy):
    """Format VocalFlash output while keeping every real task visible.

    Summary deduplication still applies to general details, but a concrete task
    is not hidden merely because the summary mentions the same action.
    """
    if not isinstance(api_data, dict) or api_data.get("ok") is not True:
        raise ValueError("Risposta API non valida")

    summary = legacy.clean_text(api_data.get("summary"))
    if not summary:
        raise ValueError("Sintesi mancante")

    tasks = api_data.get("tasks")
    task_titles = []
    if isinstance(tasks, list):
        for item in tasks:
            if isinstance(item, dict):
                title = legacy.clean_text(
                    item.get("title") or item.get("text") or item.get("value")
                )
            else:
                title = legacy.clean_text(item)
            if title:
                task_titles.append(title)

    summary_parts = [summary]
    summary_seen = [summary]
    salient_points = api_data.get("salient_points")
    if isinstance(salient_points, list):
        for item in salient_points:
            point = legacy.extract_item_value(item)
            if not point:
                continue
            if legacy.already_expressed(point, summary_seen):
                continue
            if legacy.already_expressed(point, task_titles) or legacy.already_expressed(
                point, [f"Ricordarsi di {title}" for title in task_titles]
            ):
                continue
            summary_parts.append(f"• {point}")
            summary_seen.append(point)

    detail_lines = []
    rendered_details = []
    detail_context = list(summary_seen)
    important_details = api_data.get("important_details")
    if isinstance(important_details, list):
        for item in important_details:
            value = legacy.extract_item_value(item)
            if not value:
                continue
            if isinstance(item, dict):
                status = legacy.clean_text(item.get("status")).lower()
                if status in ("proposto", "proposta", "proposed"):
                    value += " (proposto)"
                elif status in ("incerto", "incerta", "uncertain"):
                    value += " (da confermare)"
                detail_type = legacy.clean_text(item.get("type")).lower()
                if detail_type in ("appuntamento", "scadenza", "evento"):
                    value = f"{detail_type.capitalize()}: {value}"
            if not legacy.already_expressed(value, detail_context):
                detail_lines.append(f"• {value}")
                rendered_details.append(value)
                detail_context.append(value)

    # Summary is useful context for deciding whether deadline/time is already
    # visible, but it must never suppress the task title itself.
    task_context = list(detail_context)
    task_seen = list(rendered_details)
    if isinstance(tasks, list):
        for item in tasks:
            task = legacy.format_task(item, task_context)
            if task and not legacy.already_expressed(task, task_seen):
                detail_lines.append(f"• {task}")
                task_seen.append(task)

    sections = [
        "⚡ *VocalFlash*",
        "📌 *IN SINTESI*\n" + "\n".join(summary_parts),
    ]
    if detail_lines:
        sections.append(
            "🗓️ *DETTAGLI IMPORTANTI*\n" + "\n".join(detail_lines)
        )
    return "\n\n".join(sections)
