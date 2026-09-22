from enum import Enum


class CardType(Enum):
    ATTACK = 1
    SKILL = 2
    POWER = 3
    STATUS = 4
    CURSE = 5


class CardRarity(Enum):
    BASIC = 1
    COMMON = 2
    UNCOMMON = 3
    RARE = 4
    SPECIAL = 5
    CURSE = 6


class Card:
    def __init__(self, card_id, name, card_type, rarity, upgrades=0, has_target=False, cost=0, uuid="", misc=0, price=0, is_playable=False, exhausts=False, damage=0, block=0, magic_number=0, base_damage=None, base_block=None, description="", ethereal=False, in_bottle_flame=False, in_bottle_lightning=False, in_bottle_tornado=False):
        self.card_id = card_id
        self.name = name
        self.type = card_type
        self.rarity = rarity
        self.upgrades = upgrades
        self.has_target = has_target
        self.cost = cost
        self.uuid = uuid
        self.misc = misc
        self.price = price
        self.is_playable = is_playable
        self.exhausts = exhausts
        self.damage = damage
        self.block = block
        self.magic_number = magic_number
        self.base_damage = base_damage
        self.base_block = base_block
        self.description = description
        self.ethereal = ethereal
        self.in_bottle_flame = in_bottle_flame
        self.in_bottle_lightning = in_bottle_lightning
        self.in_bottle_tornado = in_bottle_tornado

    @classmethod
    def from_json(cls, json_object):
        return cls(
            card_id=json_object["id"],
            name=json_object["name"],
            card_type=CardType[json_object["type"]],
            rarity=CardRarity[json_object["rarity"]],
            upgrades=json_object["upgrades"],
            has_target=json_object["has_target"],
            cost=json_object["cost"],
            uuid=json_object["uuid"],
            misc=json_object.get("misc", 0),
            price=json_object.get("price", 0),
            is_playable=json_object.get("is_playable", False),
            exhausts=json_object.get("exhausts", False),
            damage=json_object.get("damage", 0),
            block=json_object.get("block", 0),
            magic_number=json_object.get("magic_number", 0),
            base_damage=json_object.get("base_damage"),
            base_block=json_object.get("base_block"),
            description=json_object.get("description", ""),
            ethereal=json_object.get("ethereal", False),
            in_bottle_flame=json_object.get("in_bottle_flame", False),
            in_bottle_lightning=json_object.get(
                "in_bottle_lightning", False
            ),
            in_bottle_tornado=json_object.get("in_bottle_tornado", False),
        )

    def __eq__(self, other):
        return self.uuid == other.uuid
