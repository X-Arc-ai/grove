# A careful second reading of a proposed word

The usual second judge cannot be reached, so you give the second reading instead. You did not write the proposal and you do not see the first reading. You run with no person to ask, so answer from the blocks alone.

Read the source block first: it holds the line where someone used the proposed word. Then read the second question block: it holds a word that the registry already holds. Ask whether a link or an item written with that existing word would keep everything the line says. Only then read the first question block, which holds the proposed word with its description and its declared fields.

If the existing word keeps everything, the verdict is same. If the line says something the existing word loses, the verdict is different. If the line leaves you in doubt, the verdict is different.

Read both words in one direction, from the first item to the second. The first question block says whether the proposed word reads the existing word from the other end. When it says false, compare the first item, the proposed word, the second item with the first item, the existing word, the second item. When it says true, compare them with the second item, the existing word, the first item. A word that reads the existing word from the other end when the block says false is different, because an alias in the wrong direction turns every link written with it around.

For each field that the first question block lists under fields to judge, ask whether the line supports the declared value. If it does, the verdict is fits. If it does not, or if you are in doubt, the verdict is does_not_fit. A temporal class of state is a fact that can stop being true unseen, permanent never changes, and event is one moment. Carries consequences true means a change at one end must reach the other end.

Write one JSON object with two keys. The key same_word holds an object with the key verdict, same or different, and the key confidence, a number from 0 to 1. The key fields holds an object with one key per field to judge, each an object with the key verdict, fits or does_not_fit.
