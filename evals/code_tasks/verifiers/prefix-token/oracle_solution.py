def allowed(values, wanted):
    return wanted in {item.strip() for item in values.split(",")}
