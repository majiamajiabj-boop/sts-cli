from enum import Enum

import spirecomm.spire.relic
import spirecomm.spire.card
import spirecomm.spire.character
import spirecomm.spire.map
import spirecomm.spire.potion
import spirecomm.spire.screen


class RoomPhase(Enum):
    COMBAT = 1,
    EVENT = 2,
    COMPLETE = 3,
    INCOMPLETE = 4


class Game:

    def __init__(self):

        # General state

        self.current_action = None
        self.current_hp = 0
        self.max_hp = 0
        self.floor = 0
        self.act = 0
        self.gold = 0
        self.seed = 0
        self.character = None
        self.ascension_level = None
        self.key_system_unlocked = False
        self.has_ruby_key = False
        self.has_emerald_key = False
        self.has_sapphire_key = False
        self.ironclad_third_act_win = False
        self.silent_third_act_win = False
        self.defect_third_act_win = False
        self.relics = []
        self.deck = []
        self.potions = []
        self.map = []

        # Combat state

        self.in_combat = False
        self.player = None
        self.monsters = []
        self.draw_pile = []
        # Legacy CommunicationMod exports ordered piles; offline bags opt out.
        self.draw_pile_order_known = True
        self.discard_pile = []
        self.exhaust_pile = []
        self.hand = []
        self.limbo = []
        self.card_in_play = None
        self.turn = 0
        self.cards_discarded_this_turn = 0

        # Current Screen

        self.screen = None
        self.screen_up = False
        self.screen_type = None
        self.room_phase = None
        self.room_type = None
        self.choice_list = []
        self.choice_available = False

        # Available Commands

        self.end_available = False
        self.potion_available = False
        self.play_available = False
        self.proceed_available = False
        self.cancel_available = False

    @classmethod
    def from_json(cls, json_state, available_commands):
        game = cls()
        game.current_action = json_state.get("current_action", None)
        game.current_hp = json_state.get("current_hp")
        game.max_hp = json_state.get("max_hp")
        game.floor = json_state.get("floor")
        game.act = json_state.get("act")
        game.gold = json_state.get("gold")
        game.seed = json_state.get("seed")
        game.character = spirecomm.spire.character.PlayerClass[json_state.get("class")]
        game.ascension_level = json_state.get("ascension_level")
        game.key_system_unlocked = bool(json_state.get("key_system_unlocked", False))
        game.has_ruby_key = bool(json_state.get("has_ruby_key", False))
        game.has_emerald_key = bool(json_state.get("has_emerald_key", False))
        game.has_sapphire_key = bool(json_state.get("has_sapphire_key", False))
        game.ironclad_third_act_win = bool(json_state.get("ironclad_third_act_win", False))
        game.silent_third_act_win = bool(json_state.get("silent_third_act_win", False))
        game.defect_third_act_win = bool(json_state.get("defect_third_act_win", False))
        game.relics = [spirecomm.spire.relic.Relic.from_json(json_relic) for json_relic in json_state.get("relics")]
        game.deck = [spirecomm.spire.card.Card.from_json(json_card) for json_card in json_state.get("deck")]
        game.map = spirecomm.spire.map.Map.from_json(json_state.get("map"))
        game.potions = [spirecomm.spire.potion.Potion.from_json(potion) for potion in json_state.get("potions")]
        game.act_boss = json_state.get("act_boss", None)

        # Screen State

        game.screen_up = json_state.get("is_screen_up", False)
        screen_type_name = json_state.get("screen_type")
        screen_state = json_state.get("screen_state")
        # CommunicationMod can leave a completed event dialog with
        # screen_type=NONE while room_phase remains EVENT.  The authoritative
        # event id/options are still present and the controller must press the
        # visible Continue/Leave option instead of waiting forever.  Normalize
        # only this unambiguous event surface; other NONE screens retain their
        # existing semantics.
        if (
            screen_type_name in {None, "NONE"}
            and json_state.get("room_phase") == "EVENT"
            and isinstance(screen_state, dict)
            and screen_state.get("event_id")
            and isinstance(screen_state.get("options"), list)
        ):
            screen_type_name = "EVENT"
            screen_state = dict(screen_state)
            screen_state.setdefault("event_name", screen_state.get("event_id"))
            screen_state.setdefault("body_text", "")
        game.screen_type = spirecomm.spire.screen.ScreenType[screen_type_name]
        game.screen = spirecomm.spire.screen.screen_from_json(game.screen_type, screen_state)
        game.room_phase = RoomPhase[json_state.get("room_phase")]
        game.room_type = json_state.get("room_type")
        game.choice_available = "choice_list" in json_state
        if not game.choice_available and game.screen_type == spirecomm.spire.screen.ScreenType.EVENT:
            # Some event result frames omit the legacy top-level choice_list
            # even though screen_state.options contains an enabled button.
            # Treat that typed option surface as actionable so the agent can
            # close the dialog instead of falling into transport waits.
            game.choice_available = any(
                not option.disabled and option.choice_index is not None
                for option in getattr(game.screen, "options", [])
            )
        if game.choice_available:
            game.choice_list = json_state.get("choice_list")

        # Combat state

        game.in_combat = game.room_phase == RoomPhase.COMBAT
        if game.in_combat:
            combat_state = json_state.get("combat_state")
            game.player = spirecomm.spire.character.Player.from_json(combat_state.get("player"))
            game.monsters = [spirecomm.spire.character.Monster.from_json(json_monster) for json_monster in combat_state.get("monsters")]
            for i, monster in enumerate(game.monsters):
                monster.monster_index = i
            game.draw_pile = [spirecomm.spire.card.Card.from_json(json_card) for json_card in combat_state.get("draw_pile")]
            game.draw_pile_order_known = combat_state.get("draw_pile_order_known", True) is True
            game.discard_pile = [spirecomm.spire.card.Card.from_json(json_card) for json_card in combat_state.get("discard_pile")]
            game.exhaust_pile = [spirecomm.spire.card.Card.from_json(json_card) for json_card in combat_state.get("exhaust_pile")]
            game.hand = [spirecomm.spire.card.Card.from_json(json_card) for json_card in combat_state.get("hand")]
            game.limbo = [spirecomm.spire.card.Card.from_json(json_card) for json_card in combat_state.get("limbo", [])]
            game.card_in_play = combat_state.get("card_in_play", None)
            if game.card_in_play is not None:
                game.card_in_play = spirecomm.spire.card.Card.from_json(game.card_in_play)
            game.turn = combat_state.get("turn", 0)
            game.cards_discarded_this_turn = combat_state.get("cards_discarded_this_turn", 0)

        # Available Commands

        game.end_available = "end" in available_commands
        game.potion_available = "potion" in available_commands
        game.play_available = "play" in available_commands
        game.proceed_available = "proceed" in available_commands or "confirm" in available_commands
        game.cancel_available = "cancel" in available_commands or "leave" in available_commands \
                                or "return" in available_commands or "skip" in available_commands

        return game

    def are_potions_full(self):
        for potion in self.potions:
            if potion.potion_id == "Potion Slot":
                return False
        return True

    def get_real_potions(self):
        potions = []
        for potion in self.potions:
            if potion.potion_id != "Potion Slot":
                potions.append(potion)
        return potions
