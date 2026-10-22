def choose(rules, value):
    matches = [rule for rule in rules if value >= rule["minimum"]]
    return max(matches, key=lambda rule: rule["minimum"])["name"]
