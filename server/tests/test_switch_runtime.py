from test_basic_runtime import make_case
import pytest

def responses():
    return {'save-rotate':'rotate: 0 (normal)', 'write-rotate-1':'OK',
            'verify-rotate-1':'rotate: 1 (rotated)', 'write-rotate-0':'OK',
            'verify-rotate-0':'rotate: 0 (normal)', 'restore-rotate':'OK',
            'verify-restore-rotate':'rotate: 0 (normal)'}

def test_rotation_controls_and_restores(make_case):
    case=make_case('SWITCH-ROTATE-CONTROL',responses())
    assert case.run()==0
    assert case.result['recovery']['state']=='passed'
    assert case.result['scope']=='configuration_readback_only'
    assert case.result['image_evidence']['state']=='unavailable'

@pytest.mark.parametrize('step,value,verdict',[
    ('verify-rotate-1','rotate: 0 (normal)','failed'),
    ('write-rotate-1',RuntimeError('timeout'),'error'),
    ('verify-rotate-1','garbled response','error'),
])
def test_restore_even_after_partial_failure(make_case,step,value,verdict):
    values=responses();values[step]=value
    case=make_case('SWITCH-ROTATE-CONTROL',values)
    assert case.run()==1
    assert case.result['outcome']==verdict
    assert case.result['recovery']['state']=='passed'

def test_failed_recovery_prevents_pass(make_case):
    values=responses();values['restore-rotate']=RuntimeError('device lost')
    case=make_case('SWITCH-ROTATE-CONTROL',values)
    assert case.run()==1
    assert case.result['outcome']=='error'
    assert case.result['recovery']['state']=='failed'

def test_invalid_original_does_not_write(make_case):
    case=make_case('SWITCH-ROTATE-CONTROL',{'save-rotate':'rotate: 7'})
    assert case.run()==1
    assert case.result['recovery']['state']=='not_required'
