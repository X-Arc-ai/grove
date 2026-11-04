'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2] || path.join(__dirname, '../simulation.js'), 'utf8');
const MAIN = 'grove-minimal-codex-v3', PREF = 'grove-onboarding-v1';
async function open(store = {}) {
  const box = {console, URL, Date, Math, Promise, setTimeout, clearTimeout,
    CustomEvent: class {constructor(type, init) {this.type=type; this.detail=init.detail;}},
    localStorage: {getItem:key=>store[key]||null,setItem:(key,value)=>{store[key]=value;}},
    location: {href:'https://grove.example/login'}, dispatchEvent(){}, reloadGrove(){}};
  box.window=box; vm.createContext(box); vm.runInContext(source, box); await Promise.resolve();
  return box.GroveSimulation;
}
(async()=>{
  const store={}, a=await open(store);
  a.signIn('email','unverified@example.test');a.setStep(3);
  assert.equal(store[PREF],undefined,'Browser sign-in values never establish a preference identity');
  a.bindAccount('verified-A');a.signIn('email','maya@example.test');a.setStep(2);a.completeTour();
  a.setName('Private owner');a.saveGrowth({focus:'Private draft',reading:'',avoid:'',confirm:true});a.signOut();
  let state=a.getState();
  assert.equal(state.account,null);assert.equal(state.auth,null);assert.equal(state.name,'');assert.equal(state.growth.focus,'');
  assert.equal(state.onboardingStep,0);assert.equal(state.currentNodeCount,0);
  assert.deepEqual(JSON.parse(store[PREF]),[{account:'verified-A',step:2,complete:false,tour:true}]);
  for(const text of ['Private','maya@example.test','verified-A'])assert(!store[MAIN].includes(text));
  const b=await open(store);b.bindAccount('verified-B');b.signIn('email','robin@example.test');
  assert.equal(b.getState().onboardingStep,0,'B cannot inherit A progress');b.setStep(4);b.completeOnboarding();b.signOut();
  const again=await open(store);again.bindAccount('verified-A');again.signIn('email','maya@example.test');
  assert.equal(again.getState().onboardingStep,2);assert.equal(again.getState().onboardingComplete,false);assert.equal(again.getState().tourComplete,true);
  again.signOut();again.bindAccount('verified-B');assert.equal(again.getState().onboardingComplete,true);
  for(let i=0;i<12;i++){again.signOut();again.bindAccount('verified-'+i);again.setStep(i%5);}
  assert.equal(JSON.parse(store[PREF]).length,8,'Preference account count is bounded');
  store[PREF]=JSON.stringify([{account:'verified-Z',step:4,complete:true,tour:true,draft:'forbidden'}]);
  again.signOut();again.bindAccount('verified-Z');assert.equal(again.getState().onboardingComplete,false);
  assert(!store[PREF].includes('forbidden'),'Unknown fields are never carried into saved preferences');
  store[PREF]=' '.repeat(5000);again.signOut();again.bindAccount('verified-Y');
  assert(store[PREF].length<4096,'Oversized preference input cannot be retained by a save');
  const stale={[MAIN]:JSON.stringify({version:3,schema:4,auth:{provider:'email'},account:'unverified',onboardingStep:4,name:'Private old owner'})};
  const anonymous=await open(stale);anonymous.signOut();assert.equal(stale[PREF],undefined);
  assert.equal(anonymous.getState().account,null);assert(!stale[MAIN].includes('Private old owner'));
  console.log('PASS account state: verified identity, protected cleanup, A/B isolation, A/A continuity, bounded preferences, anonymous state');
})().catch(error=>{console.error(error);process.exitCode=1;});
