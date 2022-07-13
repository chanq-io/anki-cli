from collections import namedtuple
from copy import deepcopy
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
from glob import glob
from itertools import chain
from textwrap import wrap
import argparse
import json
import os
import random
import re
import sys

Score = Enum('Score', 'FAIL HARD PASS EASY', start=1)
MarkdownFile = namedtuple('MarkdownFile', ('path', 'content'))
QuestionAnswer = namedtuple('QuestionAnswer', ('question', 'answer'))
FlashCard = namedtuple(
    'FlashCard',
    ('path', 'tags', 'question', 'answer', 'due', 'interval', 'factor')
)
Deck = namedtuple(
    'Deck',
    (
        'tag',
        'flash_cards',
        'standard_interval_modifier',
        'easy_interval_modifier',
        'fail_interval_modifier'
    )
)
AnkiState = namedtuple('AnkiState', ('decks', 'flash_cards'))
QUIT_CMDS = ['q', 'Q']

def read_markdown_files(markdown_dir):
    def read(p):
        return MarkdownFile(p, read_state(p))
    return map(read, glob(f'{markdown_dir}/*.md'))

def filter_flash_card_files(markdown_files):
    def is_flash_card(z):
        return re.search('\*\*Q\*\*', z.content)

    return filter(is_flash_card, markdown_files)

def get_tags(MarkdownFile):
    tags = re.search(r'(.*(?:tags\:\s))(.*)', MarkdownFile.content).group(2)
    return list(filter(None, tags.split(':')))

def get_question_and_answer(MarkdownFile):
    def is_not_none(x):
        return not(x is None)

    question = re.search(r'(.*(?:\*\*Q\*\*).*)', MarkdownFile.content)
    answer = re.search('\n---\n((.*?\n)*?)\*\*Q\*\*', MarkdownFile.content)
    both_truthy = all(map(is_not_none, [question, answer]))
    question = ' '.join(question[0].split(' ')[1:]).capitalize()
    return QuestionAnswer(question, answer.group(1)) if both_truthy else None

def maybe_state(state, key, fallback):
    return state[state._fields.index(key)] if state else fallback

def filter_by_tag(flash_cards, tag):
    return list(filter(lambda c: tag in c.tags, flash_cards))

def new_flash_card(MarkdownFile, state=None):
    now = datetime.now()
    path = MarkdownFile.path
    question, answer = get_question_and_answer(MarkdownFile)
    return FlashCard(
        path = path,
        tags = get_tags(MarkdownFile),
        question = question,
        answer = answer,
        due = maybe_state(state, 'due', now),
        interval = maybe_state(state, 'interval', 0),
        factor = maybe_state(state, 'factor', 1300)
    )

def update_flash_card(card, interval, factor, due):
    return FlashCard(card.path, card.tags, card.question, card.answer, due, interval, factor)

def new_deck(tag, flash_cards, state=None):
    return Deck(
        tag = tag,
        flash_cards = [c.path for c in flash_cards],
        standard_interval_modifier = maybe_state(state, 'standard_interval_modifier', 1.0),
        easy_interval_modifier = maybe_state(state, 'easy_interval_modifier', 1.3),
        fail_interval_modifier = maybe_state(state, 'fail_interval_modifier', 0.0)
    )

def serialise(flash_cards, deck, state=AnkiState({}, {})):
    def card_as_dict(c):
        return {**c._asdict(), 'due': str(c.due)}

    new_state = AnkiState(
        flash_cards = {
            **maybe_state(state, 'flash_cards', {}),
            **{path: card_as_dict(card) for path, card in flash_cards.items()}
        },
        decks = {
            **{t: d._asdict() for t, d in maybe_state(state, 'decks', {}).items() },
            deck.tag: deck._asdict()
        }
    )

    return json.dumps(new_state._asdict(), indent=4, sort_keys=True)

def deserialise(state_json):
    def card_from_dict(d):
        return FlashCard(**{
            **d,
            'due': datetime.fromisoformat(d['due']),
            'interval': float(d['interval']),
            'factor': float(d['factor'])
        })

    def deck_from_dict(d):
        return Deck(**{
            **d,
            'standard_interval_modifier':float(d['standard_interval_modifier']),
            'easy_interval_modifier':float(d['easy_interval_modifier']),
            'fail_interval_modifier':float(d['fail_interval_modifier']),
        })


    state = json.loads(state_json)
    return AnkiState(
        decks = {tag: deck_from_dict(deck) for tag, deck in state.get('decks', {}).items()},
        flash_cards = {
            path: card_from_dict(card) for path, card in state.get('flash_cards', {}).items()
        }
    )

def write_state(path, state):
    with open(path, 'w') as f:
        f.write(state)

def read_anki_state(path):
    try:
        return read_state(path)
    except FileNotFoundError:
        return '{}'

def read_state(path):
    with open(path, 'r') as f:
        return f.read()

def calculate_new_interval(score, card, deck):
    # see https://gist.github.com/fasiha/31ce46c36371ff57fdbc1254af424174
    i = card.interval
    m = deck.standard_interval_modifier
    m0 = deck.fail_interval_modifier
    m4 = deck.easy_interval_modifier
    f = card.factor
    d = (datetime.now() - card.due).days
    i1 = m0 * i
    i2 = max(i + 1, (i + d / 4.0) * 1.2 * m)
    i3 = max(i2 + 1, (i + d / 2.0) * (f / 1000.0) * m)
    i4 = max(i3 + 1, (i + d) * (f / 1000.0) * m * m4)
    return {Score.FAIL: i1, Score.HARD: i2, Score.PASS: i3, Score.EASY: i4}[score]

def calculate_new_factor(score, card):
    # see https://gist.github.com/fasiha/31ce46c36371ff57fdbc1254af424174
    return {
        Score.FAIL: max(1300, card.factor - 200),
        Score.HARD: max(1300, card.factor - 150),
        Score.PASS: card.factor,
        Score.EASY: max(1300, card.factor + 150)
    }[score]

def run_repl(deck, flash_cards, state, state_path, print_summary_and_exit, table_content_cols=120):
    def clear_console():
        print("\033c", end="")

    def print_gutter(n):
        print('\n' * int(n))

    def print_vertical_offset(term_size, text):
        print_gutter(term_size.lines / 2 - (text.count('\n')+1) / 2)

    def print_center(text, term_size):
        print(text.center(term_size.columns))

    def handle_exit(term_size):
        if not print_summary_and_exit:
            write_state(state_path, serialise(flash_cards, deck, state))
        read_user_exit_command(term_size)
        sys.exit(0)

    def handle_question_input(user_input, term_size):
        if user_input in QUIT_CMDS:
            handle_exit(term_size)

    def handle_answer_input(card_id, user_input, term_size):
        def handle_score(s):
            score = Score(int(s))
            card = flash_cards[card_id]
            interval = calculate_new_interval(score, card, deck)
            factor = calculate_new_factor(score, card)
            due = datetime.now() + timedelta(interval)
            return interval, factor, due

        if user_input in QUIT_CMDS:
            handle_exit(term_size)
        return handle_score(user_input)

    def center_line(text, term_size):
        return text.center(term_size.columns) + '\n'

    def due_cards(deck, flash_cards):
        now = datetime.now()
        paths_and_cards = filter(lambda x: x[0] in deck.flash_cards, flash_cards.items())
        deck_cards = map(lambda x: x[1], paths_and_cards)
        return list(filter(lambda c: c.due <= now, deck_cards))

    def read_user_command(callback, valid_commands, term_size):
        while True:
            clear_console()
            prompt = callback()
            print_vertical_offset(term_size, prompt)
            user_input = input(prompt)
            if user_input in valid_commands:
                return user_input

    def read_quiz_command(
        message, valid_commands, command_instructions, remaining, due, term_size, center_text=True
    ):
        def create_prompt():
            return format_quiz_table(
                message,
                command_instructions,
                remaining,
                due,
                term_size,
                center_text=center_text
            )
        return read_user_command(create_prompt, valid_commands, term_size)

    def read_user_exit_command(term_size):
        def determine_decks_info(decks, cards):
            def get_info(deck):
                paths_and_cards = filter(lambda x: x[0] in deck.flash_cards, cards.items())
                deck_cards = list(map(lambda x: x[1], paths_and_cards))
                n_cards = len(deck_cards)
                next_due = next(iter(sorted(deck_cards, key=lambda x: x.due))).due
                return f'DECK(TAG = {deck.tag}, DECK_SIZE = {n_cards}, NEXT_DUE = {next_due.ctime()})'
            return 'Anki Summary\n\n'+'\n'.join(map(get_info, decks.values()))

        def create_prompt():
            command = '(Q) Quit'
            text = determine_decks_info(state.decks, state.flash_cards)
            return (
                make_table_content_area(text, table_content_cols, term_size, True)
                  + make_last_table_rows(command, table_content_cols, term_size)
            )

        return read_user_command(create_prompt, ['q', 'Q'], term_size)

    def read_user_question_command(card, remaining, due, term_size):
        return read_quiz_command(
            card.question,
            ['a', 'A'] + QUIT_CMDS,
            '(A) Answer    (Q) Quit',
            remaining,
            due,
            term_size)


    def read_user_answer_command(card, remaining, due, term_size):
        return read_quiz_command(
            card.answer,
            ['1', '2', '3', '4'] + QUIT_CMDS,
            '(1) Fail  ┃  (2) Hard  ┃  (3) Pass  ┃ (4) Easy  ┃  (Q) Quit',
            remaining,
            due,
            term_size,
            center_text=False)

    def make_table_content_area(message, table_content_cols, term_size, center_text):
        def wrap_and_align(text, columns, term_size):
            def fmt(l, spacer, width):
                t = l.center(width) if center_text else l.ljust(width)
                return f'┃{spacer}{t}{spacer}┃'.center(term_size.columns)

            gutter = 6
            width = columns - (2 + gutter * 2)
            spacer = ' ' * gutter
            return '\n'.join([fmt(l, spacer, width) for l in text.split('\n')])

        return (
            center_line(f'┏{"━"*table_content_cols}┓', term_size) +
            center_line(f'┃{" "*table_content_cols}┃', term_size) +
            center_line(f'┃{" "*table_content_cols}┃', term_size) +
            wrap_and_align(message, table_content_cols + 2, term_size) +
            center_line(f'┃{" "*table_content_cols}┃', term_size) +
            center_line(f'┃{" "*table_content_cols}┃', term_size)
        )
    def make_last_table_rows(command, table_content_cols, term_size):
        return (
            center_line(f'┣{"━"*table_content_cols}┫', term_size) +
            center_line(f'┃{command.center(table_content_cols)}┃', term_size)
            + center_line(f'┗{"━"*table_content_cols}┛', term_size)
        )

    def format_quiz_table(message, command, remaining, due, term_size, center_text=True):
        s, d, r = len(deck.flash_cards), due, remaining
        return (
            make_table_content_area(message, table_content_cols, term_size, center_text) +
            center_line(f'┣{"━"*table_content_cols}┫', term_size) +
            center_line(
                    f'┃' +
                    f'Deck ({deck.tag})  ┃  Deck Size: {s:03d}  ┃  Due: {d:03d}  ┃ Remaining: {r:03d}'.center(table_content_cols) +
                    '┃'
                , term_size) +
            make_last_table_rows(command, table_content_cols, term_size)
        )

    term_size = os.get_terminal_size()

    if print_summary_and_exit:
        handle_exit(term_size)
    try:
        shuffled_cards = sorted(due_cards(deck, flash_cards), key=lambda _: random.random())

        due = len(shuffled_cards)
        remaining = due
        while remaining > 0:
            card = shuffled_cards.pop(0)
            question_command = read_user_question_command(card, remaining, due, term_size)
            handle_question_input(question_command, term_size)
            card = update_flash_card(
                card,
                *handle_answer_input(
                    card.path,
                    read_user_answer_command(card, remaining, due, term_size),
                    term_size)
            )
            flash_cards[card.path] = card
            if card.interval == 0:
                shuffled_cards.append(card)
            remaining = len(shuffled_cards)
        handle_exit(term_size)

    except KeyboardInterrupt:
        handle_exit(term_size)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("flash_cards_dir", help="directory containing markdown flash cards")
    parser.add_argument("deck_tag", nargs='?', default=None, help="tag to filter cards by")
    parser.add_argument("-w", "--content-width", default=120, help="width of content pane")
    parser.add_argument("-s", "--summary", help="print information about anki decks state and exit", action="store_true")
    return parser.parse_args()

def main(deck_tag, flash_cards_dir, print_summary_and_exit, content_width):
    flash_card_markdown_files = filter_flash_card_files(read_markdown_files(flash_cards_dir))
    anki_state_path = f'{flash_cards_dir}/.anki-state.json'
    anki_state = deserialise(read_anki_state(anki_state_path))
    flash_cards = {
        z.path: new_flash_card(z, anki_state.flash_cards.get(z.path))
        for z in flash_card_markdown_files
    }
    deck = new_deck(
        deck_tag,
        filter_by_tag(flash_cards.values(), deck_tag),
        anki_state.decks.get(deck_tag)
    )
    run_repl(
        deck if deck is not None else None,
        flash_cards,
        anki_state,
        anki_state_path,
        print_summary_and_exit,
        table_content_cols=content_width
    )

if __name__ == '__main__':
    args = parse_args()
    main(args.deck_tag, args.flash_cards_dir, args.summary, args.content_width)
