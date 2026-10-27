import test from 'node:test';
import assert from 'node:assert/strict';
import {resolveAppearance} from '../src/appearance.mjs';
import * as policy from '../src/policy.mjs';

test('Night uses its real night picture; Day and Dusk keep their own art',()=>{
 const sun={sunrise:6,sunset:18};
 assert.equal(resolveAppearance('Night',21,sun).art,'night');
 assert.equal(resolveAppearance('Auto',99e6,sun).art,'night');
 assert.equal(resolveAppearance('Day',21,sun).art,'day');
 assert.equal(resolveAppearance('Dusk',21,sun).art,'dusk');
});
test('Recently opened shows real visible apps in saved order, unique and bounded',()=>{
 const apps=[{Id:'a',Name:'A'},{Id:'b',Name:'B'},{Id:'c',Name:'C'}];
 assert.equal(typeof policy.recentApps,'function');
 assert.deepEqual(policy.recentApps(['b','gone','b',null,'a','c'],apps,2),[apps[1],apps[0]]);
 assert.deepEqual(policy.recentApps([],apps),[]);
 assert.deepEqual(policy.recentApps(null,apps),[]);
 assert.deepEqual(policy.recentApps(['gone'],apps),[]);
});
