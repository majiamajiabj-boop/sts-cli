import copy
import unittest
from tests.test_independent_oracle import combat_choice_card, combat_choice_state, combat_choice_record, combat_choice_audit

class DeadBranchChoiceTests(unittest.TestCase):
    def records(self, mechanism='exhaust', *, branch=True, extra=False, generated_pile='hand'):
        selected=combat_choice_card('Defend_R','selected')
        kept=combat_choice_card('Strike_R','kept')
        source=combat_choice_card('Burning Pact' if mechanism=='exhaust' else 'Warcry','source')
        source.update(magic_number=0,exhausts=mechanism!='exhaust')
        generated=combat_choice_card('Seeing Red','generated');generated.update(cost=1,rarity='UNCOMMON')
        action='ExhaustAction' if mechanism=='exhaust' else 'PutOnDeckAction'
        deck=[selected,kept,source]
        before=combat_choice_state(10,'HAND_SELECT',action,deck=deck,hand=[selected,kept],draw=[],discard=[],visible=[selected,kept],selected=[],max_cards=1)
        middle=combat_choice_state(11,'HAND_SELECT',action,deck=deck,hand=[kept],draw=[],discard=[],visible=[kept],selected=[selected],max_cards=1)
        hand=[kept];draw=[] if mechanism=='exhaust' else [selected];discard=[source] if mechanism=='exhaust' else []
        exhaust=[selected] if mechanism=='exhaust' else [source]
        {'hand':hand,'draw_pile':draw,'discard_pile':discard}[generated_pile].append(generated)
        if extra:
            second=copy.deepcopy(generated);second['card_instance_id']='extra';hand.append(second)
        after=combat_choice_state(12,'COMBAT_TURN_1',None,deck=deck,hand=hand,draw=draw,discard=discard,exhaust=exhaust)
        for state in (before,middle,after):
            state['game_state']['relics']=[{'id':'Dead Branch'}] if branch else []
        return [combat_choice_record(before,middle,card=selected),combat_choice_record(middle,after,command='proceed')]

    def test_one_exhaust_generates_exactly_one_new_combat_card(self):
        for mode in ('exhaust','put'):
            with self.subTest(mode=mode):
                result=combat_choice_audit(self.records(mode))['settlements'][0]
                self.assertEqual('clear',result['status'],result)
                self.assertEqual(['generated'],result['generated_dead_branch'])

    def test_unbound_extra_and_wrong_destination_still_fail(self):
        for mode in ('exhaust','put'):
            for mutation in ({'branch':False},{'extra':True},{'generated_pile':'draw_pile'},{'generated_pile':'discard_pile'}):
                with self.subTest(mode=mode,mutation=mutation):
                    result=combat_choice_audit(self.records(mode,**mutation))['settlements'][0]
                    self.assertEqual('issues',result['status'],result)
