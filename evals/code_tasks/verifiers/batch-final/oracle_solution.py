def batches(values, size):
    return [values[index : index + size] for index in range(0, len(values), size)]
