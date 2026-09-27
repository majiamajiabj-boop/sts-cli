import copy
import unittest
import autoplay
import bridge
import independent_oracle as oracle

class TombProgressTests(unittest.TestCase):
    def state(self,stage='RESULT'):
        return {'in_game':True,'ready_for_command':True,'available_commands':['choose','state'],
                'game_state':{'ascension_level':0,'screen_type':'EVENT','room_phase':'INCOMPLETE','action_phase':'WAITING_ON_USER',
                'potions':[],'screen_state':{'event_id':'Tomb of Lord Red Mask','event_class':'com.megacrit.cardcrawl.events.beyond.TombRedMask',
                'event_stage':stage,'options':[{'choice_index':0,'original_button_index':0,'disabled':False,'label':'离开','text':'离开'}]},'choice_list':['离开']}}
    def test_result_stage_binds_leave_in_producer_and_oracle(self):
        state=bridge.enrich_state(self.state(),42);target=state['options'][0]['target']
        self.assertEqual('RESULT',target['event_stage'])
        value=autoplay._empty_consequence();expected=oracle._oracle_empty_consequence()
        self.assertTrue(autoplay._apply_indexed_a0_event_consequence(value,target,state,'tomboflordredmask'))
        self.assertTrue(oracle._oracle_apply_indexed_a0_event(expected,target,{'authoritative_state_before':state},'tomboflordredmask'))
        self.assertTrue(value['leave']);self.assertTrue(expected['leave'])
        self.assertEqual(0,value['hp_delta']);self.assertEqual(0,value['gold_delta'])
    def test_missing_or_wrong_stage_class_does_not_authorize_leave(self):
        for field,bad in [('event_stage',None),('event_class','modded.TombRedMask')]:
            state=self.state();state['game_state']['screen_state'][field]=bad
            state=bridge.enrich_state(state,43);target=state['options'][0]['target']
            self.assertFalse(autoplay._apply_indexed_a0_event_consequence(autoplay._empty_consequence(),target,state,'tomboflordredmask'))
            self.assertFalse(oracle._oracle_apply_indexed_a0_event(oracle._oracle_empty_consequence(),target,{'authoritative_state_before':state},'tomboflordredmask'))
