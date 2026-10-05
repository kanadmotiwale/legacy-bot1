"""Synthetic member data. Every value here is fake.

SSNs use the 900-series (never issued by the SSA) so they are obviously synthetic.
Some values are deliberately "planted" so the redaction tests can grep for them.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Account:
    suffix: str
    type: str
    nickname: str
    balance: Decimal
    opened: str


@dataclass
class Member:
    member_id: str
    full_name: str
    dob: str
    ssn: str
    phone: str
    address: str
    branch: str
    status: str
    restricted: bool = False
    accounts: list[Account] = field(default_factory=list)


_SEED: dict[str, Member] = {
    "10042": Member(
        member_id="10042",
        full_name="Dana Q. Whitfield",
        dob="03/14/1984",
        ssn="900-55-1234",
        phone="(555) 010-4477",
        address="12 Synthetic Ln, Springfield",
        branch="Main St",
        status="Active",
        accounts=[
            Account("S01", "Share Savings", "Primary Savings", Decimal("12345.67"), "06/01/2009"),
            Account("C10", "Checking", "Everyday", Decimal("2210.05"), "06/01/2009"),
        ],
    ),
    "10077": Member(
        member_id="10077",
        full_name="Ravi P. Okonkwo",
        dob="11/02/1991",
        ssn="900-66-9876",
        phone="(555) 010-8821",
        address="400 Placeholder Ave, Shelbyville",
        branch="Riverside",
        status="Active",
        accounts=[
            Account("S01", "Share Savings", "Primary Savings", Decimal("845.10"), "02/17/2015"),
        ],
    ),
    "10099": Member(
        member_id="10099",
        full_name="Morgan T. Alvarez",
        dob="07/30/1975",
        ssn="900-77-4321",
        phone="(555) 010-3300",
        address="9 Example Ct, Ogdenville",
        branch="Main St",
        status="Active",
        restricted=True,  # employee account: lookups by tellers are denied
        accounts=[
            Account("S01", "Share Savings", "Primary Savings", Decimal("50210.00"), "09/09/2001"),
        ],
    ),
}


class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.members: dict[str, Member] = copy.deepcopy(_SEED)
        self.ref_counter = 480_000

    def next_reference(self) -> str:
        self.ref_counter += 17
        return f"SA-{self.ref_counter}"
