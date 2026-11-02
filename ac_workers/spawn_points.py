"""Six start points spread around one lap."""


def spawn_fraction(worker_id, rng=None, randomize=True):
    slot = int(worker_id) % 6
    if not randomize:
        return (slot + 0.5) / 6.0
    roll = rng.random() if rng is not None else __import__("random").random()
    return (slot + float(roll)) / 6.0
