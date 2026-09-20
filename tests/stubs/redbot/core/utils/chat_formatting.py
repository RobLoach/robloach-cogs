def pagify(text, delims=("\n",), *, page_length=2000, **kwargs):
    while text:
        yield text[:page_length]
        text = text[page_length:]


def humanize_list(items, *, language="en", style="standard"):
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
