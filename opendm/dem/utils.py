
def get_dem_vars(args):
    return {
        'TILED': 'YES',
        'COMPRESS': 'LZW',
        'BLOCKXSIZE': 512,
        'BLOCKYSIZE': 512,
        'NUM_THREADS': args.max_concurrency,
    }
