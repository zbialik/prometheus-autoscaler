# extracted from: https://github.com/kubernetes-client/python/blob/2f34a1ce9491cf9332f581b2207b72f0d0ab8f78/kubernetes/utils/quantity.py
import asyncio
from decimal import Decimal, InvalidOperation
from math import ceil

def stringToBool(boolString:str) -> bool:
    boolString = boolString.strip().upper()
    if boolString == 'TRUE':
        return True
    elif boolString == 'FALSE':
        return False
    else:
        raise Exception(f"boolString must be either TRUE or FALSE but is {boolString}.")

def sizeof_fmt(num:Decimal, suffix="B"):
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < Decimal(1024.0):
            return f"{num:3.1f} {unit}{suffix}"
        num /= Decimal(1024.0)
    return f"{num:.1f} Yi{suffix}"

async def sleep_and_log(waitTime:int, logger):
    interval = 5 # 5 second intervals

    loops = ceil(waitTime / 5) 
    waitTime = loops * interval 

    logger.info(f"waiting total of {waitTime} seconds")
    for i in range(loops):
        await asyncio.sleep(interval)
        logger.info(f"waited {str(interval * (i+1))} out of {waitTime}s")

def parse_quantity(quantity) -> Decimal:
    """
    Parse kubernetes canonical form quantity like 200Mi to a decimal number.
    Supported SI suffixes:
    base1024: Ki | Mi | Gi | Ti | Pi | Ei
    base1000: n | u | m | "" | k | M | G | T | P | E
    See https://github.com/kubernetes/apimachinery/blob/master/pkg/api/resource/quantity.go
    Input:
    quantity: string. kubernetes canonical form quantity
    Returns:
    Decimal
    Raises:
    ValueError on invalid or unknown input
    """
    if isinstance(quantity, (int, float, Decimal)):
        return Decimal(quantity)

    exponents = {"n": -3, "u": -2, "m": -1, "K": 1, "k": 1, "M": 2,
                 "G": 3, "T": 4, "P": 5, "E": 6}

    quantity = str(quantity)
    number = quantity
    suffix = None
    if len(quantity) >= 2 and quantity[-1] == "i":
        if quantity[-2] in exponents:
            number = quantity[:-2]
            suffix = quantity[-2:]
    elif len(quantity) >= 1 and quantity[-1] in exponents:
        number = quantity[:-1]
        suffix = quantity[-1:]

    try:
        number = Decimal(number)
    except InvalidOperation:
        raise ValueError("Invalid number format: {}".format(number))

    if suffix is None:
        return number

    if suffix.endswith("i"):
        base = 1024
    elif len(suffix) == 1:
        base = 1000
    else:
        raise ValueError("{} has unknown suffix".format(quantity))

    # handly SI inconsistency
    if suffix == "ki":
        raise ValueError("{} has unknown suffix".format(quantity))

    if suffix[0] not in exponents:
        raise ValueError("{} has unknown suffix".format(quantity))

    exponent = Decimal(exponents[suffix[0]])
    return number * (base ** exponent)