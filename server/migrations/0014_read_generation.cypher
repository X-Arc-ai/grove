MATCH (w:World) WHERE w.read_generation IS NULL SET w.read_generation = 0;
