import copy
import unittest
import independent_oracle as oracle

class GridProjectionFactsTests(unittest.TestCase):
    def test_grid_projection_preserves_and_checks_protocol_v3_card_facts(self):
        card=dict(id='Strike_R',name='打击',card_instance_id='card-1',upgrades=0,
                  misc=0,combat_cost=1,free_to_play_once=False,retain=False,ethereal=False)
        record={'authoritative_state_before':{'game_state':{'screen_state':{
            'for_upgrade':False,'for_transform':True,'for_purge':False,'cards':[card]}}}}
        target={'kind':'card','card_instance_id':'card-1','card':copy.deepcopy(card)}
        operation,selected,error=oracle._oracle_grid_target(record,target)
        self.assertIsNone(error)
        self.assertEqual('grid_transform',operation)
        self.assertEqual(card,selected)
        for field in ('misc','combat_cost','free_to_play_once','retain','ethereal'):
            with self.subTest(field=field):
                altered=copy.deepcopy(target)
                altered['card'][field]=not card[field] if type(card[field]) is bool else card[field]+1
                self.assertEqual('grid_target_card_facts_mismatch',oracle._oracle_grid_target(record,altered)[2])

    def test_producer_retains_observed_v3_facts_in_selected_card_claim(self):
        import autoplay  # installs the bundled spirecomm import path
        from spirecomm.ai.agent import SimpleAgent
        from spirecomm.spire.card import Card
        raw=dict(id='Strike_R',name='打击',uuid='card-1',type='ATTACK',rarity='BASIC',
                 upgrades=0,has_target=True,cost=1,misc=7,combat_cost=0,
                 free_to_play_once=True,retain=True,ethereal=True)
        card=Card.from_json(raw)
        facts=SimpleAgent()._audit_card_instance_facts(card)
        for field in ('misc','combat_cost','free_to_play_once','retain','ethereal'):
            self.assertEqual(raw[field],facts[field])
        # Earlier protocol fixtures without these fields stay explicitly absent.
        legacy={k:v for k,v in raw.items() if k not in ('misc','combat_cost','free_to_play_once','retain','ethereal')}
        legacy_facts=SimpleAgent()._audit_card_instance_facts(Card.from_json(legacy))
        self.assertNotIn('combat_cost',legacy_facts)
