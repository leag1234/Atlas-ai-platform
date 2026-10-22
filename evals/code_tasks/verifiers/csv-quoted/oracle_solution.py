def fields(text):
    import csv

    return next(csv.reader([text], skipinitialspace=True))
