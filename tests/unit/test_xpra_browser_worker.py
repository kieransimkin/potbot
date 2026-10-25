"""Deterministic CDP/ARIA inspector behavior without a live graphical server."""
import asyncio

from potbot.tools.xpra_browser_worker import BrowserWorker, _safe_url


class FakeCDP:
    def __init__(self):
        self.commands = []
    async def send(self, method, params=None):
        self.commands.append((method, params))
        if method == 'DOM.pushNodesByBackendIdsToFrontend':return {'nodeIds':[12]}
        if method == 'DOM.describeNode':return {'node':{'nodeName':'BUTTON','attributes':['id','save','aria-label','Save','value','a secret']}}
        if method == 'Accessibility.getPartialAXTree':return {'nodes':[{'role':{'value':'button'},'name':{'value':'Save'},'ignored':False}]}
        if method == 'CSS.getMatchedStylesForNode':return {'matchedCSSRules':[{'rule':{'selectorList':{'text':'button.primary'},'style':{'styleSheetId':'sheet-one','range':{'startLine':38}}}}]}
        if method == 'CSS.getComputedStyleForNode':return {'computedStyle':[{'name':'display','value':'flex'},{'name':'color','value':'rgb(1,2,3)'},{'name':'private-property','value':'no'}]}
        if method == 'DOM.getBoxModel':return {'model':{'width':90,'height':40,'content':[0,0,90,0,90,40,0,40]}}
        if method == 'DOM.resolveNode':return {'object':{'objectId':'opaque-id'}}
        if method == 'DOMDebugger.getEventListeners':return {'listeners':[{'type':'click','scriptId':'41','lineNumber':55,'columnNumber':0}]}
        if method == 'DOM.getNodeForLocation':return {'backendNodeId':5}
        return {}


def test_inspect_collects_css_events_ax_without_input_value():
    async def run():
        worker = BrowserWorker('playwright', False)
        worker.cdp = FakeCDP()
        worker._sheets['sheet-one']={'sourceURL':'https://app.example.test/static/ui.css?token=secret',
                                     'sourceMapURL':'https://app.example.test/ui.css.map'}
        result = await worker.inspect(5)
        assert result['tag']=='BUTTON'
        assert result['attributes']=={'id':'save','aria-label':'Save'}
        assert result['accessibility'][0]['name']=='Save'
        assert result['styles'][0]['source']=='https://app.example.test/static/ui.css'
        assert result['styles'][0]['sourceMap'].endswith('ui.css.map')
        assert result['styles'][0]['line']==38
        assert result['events'][0]['scriptId']=='41'
        assert result['computed']['display']=='flex'
        assert 'private-property' not in result['computed']
        return worker
    asyncio.run(run())


def test_devtools_picker_uses_overlay_and_hit_test():
    async def run():
        worker=BrowserWorker('playwright',False)
        worker.cdp=FakeCDP()
        assert (await worker.execute({'op':'inspect.start'}))['picker'] is True
        worker._selected.append(5)
        selected=await worker.execute({'op':'inspect.poll'})
        assert selected['selected']['backendNodeId']==5
        assert (await worker.execute({'op':'inspect.at','x':12,'y':18}))['selected']['tag']=='BUTTON'
        assert (await worker.execute({'op':'inspect.stop'}))['picker'] is False
        assert any(k=='Overlay.setInspectMode' for k,_ in worker.cdp.commands)
        assert any(k=='DOM.getNodeForLocation' for k,_ in worker.cdp.commands)
    asyncio.run(run())


def test_url_projection_omits_query_fragment_and_basic_auth():
    assert _safe_url('https://user:pass@app.example.test:8443/page?token=secret#login') == \
           'https://app.example.test:8443/page'
