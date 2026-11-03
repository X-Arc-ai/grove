# Second opinion on a proposed word

You are the second judge of one proposed word. Another model judged it first, and you do not see that judgment. You run with no person to ask, so answer from the blocks alone.

The first question block holds the proposed word: its type, its name, its description and its declared fields. The second question block holds the nearest existing word. The source block holds the line where the proposed word was used.

First, judge the word. Answer same when the existing word says everything that the source line says with the proposed word. Answer different when the proposed word says something that the existing word cannot say. When you are not sure, answer different.

Read both words in one direction, from the first item to the second. The first question block says whether the proposed word reads the existing word from the other end. When it says false, compare the first item, the proposed word, the second item with the first item, the existing word, the second item. When it says true, compare them with the second item, the existing word, the first item. A word that reads the existing word from the other end when the block says false is different, because an alias in the wrong direction turns every link written with it around.

Then judge each declared field that the first question block lists under fields to judge. Answer fits when the source line supports the declared value. Answer does_not_fit when it does not, or when you are not sure.

These are the fields you can be asked about:
- temporal_class: state means the fact can stop being true with no one telling Grove, such as where someone lives. permanent means it never changes once true, such as who a person's parent is. event means it records one moment.
- carries_consequences: true means a change to one end must reach the other end, as a task that depends on a plan must change when the plan changes.
- status_vocabulary: each status of the kind maps to one role, such as open, active, blocked, done or cancelled. Judge whether each mapping is right.
- proposed_capability: the source named in the value can confirm, show as said, or show as planned a fact written with the word.

Write one JSON object with two keys. The key same_word holds an object with the key verdict, which is same or different, and the key confidence, a number from 0 to 1. The key fields holds an object with one key per field to judge. Each holds an object with the key verdict, which is fits or does_not_fit.
