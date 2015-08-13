var Discord = require('node-discord');
var request = require('request');
var fs = require('fs');

var config = {};

function load_config() {
    config = JSON.parse(fs.readFileSync('config.json').toString());
}


load_config();

var bot = new Discord({
    username: config.username,
    password: config.password,
    chats: config.chats,
    autorun: true
});

var commands = {};

var authority_prettify = {
    0: "User",
    1: "Moderator",
    2: "Admin",
    3: "Creator"
};


/* begin polyfill */

if (!Array.prototype.find) {
  Array.prototype.find = function(predicate) {
    if (this === null) {
      throw new TypeError('Array.prototype.find called on null or undefined');
    }
    if (typeof predicate !== 'function') {
      throw new TypeError('predicate must be a function');
    }
    var list = Object(this);
    var length = list.length >>> 0;
    var thisArg = arguments[1];
    var value;

    for (var i = 0; i < length; i++) {
      value = list[i];
      if (predicate.call(thisArg, value, i, list)) {
        return value;
      }
    }
    return undefined;
  };
}

/* end polyfill */

function get_user_authority(userID) {
    return config.authority[userID] || 0;
}

// So.
// A command can be either a function or a dictionary
// A dictionary can have the following properties:
// 'hidden' -- specifies that it doesn't show up in !help
// 'authority' -- specifies that the userID must have x authority or higher, default 0.
// 'command' -- specifies the actual function to be called
// 'help' -- the help text given when !help is called

function get_command_from_message(message) {
    if(message.indexOf(config.command_prefix) !== 0) {
        return;
    }

    var command = message.substr(config.command_prefix.length);

    if(command in commands) {
        return commands[command];
    }
}

function command_is_hidden(key) {
    var command = commands[key];
    if(typeof(command) === 'object') {
        return command.hidden || command.authority == 3;
    }
    return false;
}

commands.help = {
    help: 'shows this message',
    command: function(opts) {
        var text = 'available commands:\n';
        for(var key in commands) {
            if(command_is_hidden(key)) {
                continue;
            }
            if(commands[key].help) {
                text = text.concat(config.command_prefix, key, ' -- ', commands[key].help, '\n');
            }
            else {
                text = text.concat(config.command_prefix, key, '\n');
            }
        }
        bot.sendMessage({ to: opts.chatID, message: text });
    }
};

commands.hello = {
    help: 'displays my intro message!',
    command: function(opts) {
        bot.sendMessage({ to: opts.chatID, message: "Hello! I'm a robot! Danny made me."  });
    }
};

commands.random = {
    help: 'displays a random weapon, map, or number',
    command: function(opts) {
        var error_string = 'Random what? weapon, map, or number? (e.g. !random weapon)'
        if(opts.words.length < 1) {
            bot.sendMessage({ to: opts.chatID, message: error_string });
            return;
        }

        var type = opts.words[0].toLowerCase();
        if(type == 'weapon') {
            var index = Math.floor(Math.random() * config.splatoon.weapons.length);
            bot.sendMessage({ to: opts.chatID, message: config.splatoon.weapons[index] });
        }
        else if(type == 'number') {
            bot.sendMessage({ to: opts.chatID, message: Math.floor(Math.random() * 100).toString() });
        }
        else if(type == 'map') {
            var index = Math.floor(Math.random() * config.splatoon.maps.length);
            bot.sendMessage({ to: opts.chatID, message: config.splatoon.maps[index] });
        }
        else {
            bot.sendMessage({ to: opts.chatID, message: error_string });
        }
    }
};

function get_splatoon_map_callback(index, prefix) {
    return function(error, response, body) {
        if(error || response.statusCode != 200) {
            bot.sendMessage({ to: opts.chatID, message: "Unfortunately an error occurred. Tell Danny the error was " + error });
            return;
        }

        var data = JSON.parse(body);
        var schedule = data['schedule'];
        if(!schedule || schedule.length == 0) {
            bot.sendMessage({ to: opts.chatID, message: "Maps could not be found..." });
            return;
        }

        var current_maps = schedule[index];
        var result = '';
        result = result.concat(prefix, ' regular maps: ', current_maps.regular.maps[0].nameEN, ' and ', current_maps.regular.maps[1].nameEN, '\n');
        result = result.concat(prefix, ' ', current_maps.ranked.rulesEN, ' maps: ', current_maps.ranked.maps[0].nameEN, ' and ', current_maps.ranked.maps[1].nameEN);
        bot.sendMessage({ to: opts.chatID, message: result });
    };
}

function to_percent(value) {
    return (value * 100).toString() + '%';
}

commands.maps = {
    help: 'shows the current Splatoon maps in rotation',
    command: function(opts) {
        request(config.splatoon.schedule_url, get_splatoon_map_callback(0, 'Current'));
    }
};

commands.nextmaps = {
    help: 'shows the next Splatoon maps in rotation',
    command: function(opts) {
        request(config.splatoon.schedule_url, get_splatoon_map_callback(1, 'Next'));
    }
};

commands.index = {
    authority: 3,
    command: function(opts) {
        bot.sendMessage({ to: opts.chatID, message: 'Chat ID: ' + opts.chatID + ' userID: ' + opts.userID + ' username: ' + opts.user });
    }
};

commands.raw = {
    authority: 3,
    command: function(opts) {
        bot.sendMessage({ to: opts.chatID, message: JSON.stringify(opts.rawEvent) });
    }
};

commands.quit = {
    authority: 3,
    command: function(opts) {
        bot.disconnect();
        process.exit(0);
    }
};

commands.choose = {
    help: 'helps choose between multiple choices',
    command: function(opts) {
        if(opts.words.length < 2) {
            bot.sendMessage({ to: opts.chatID, message: 'Not enough choices to choose from... (e.g. !choose 1 2 3)' });
            return;
        }

        var random_index = Math.floor(Math.random() * opts.words.length);
        bot.sendMessage({ to: opts.chatID, message: opts.words[random_index] });
    }
};

commands.brand = {
    help: 'shows info about a splatoon brand',
    command: function(opts) {
        var input = opts.words.join(' ');
        var lower_case_input = input.toLowerCase();

        if(lower_case_input == 'list') {
            bot.sendMessage({ to: opts.chatID, message: config.splatoon.brands.map(function(arg) { return arg.name; }).join(', ') });
            return;
        }
        var brand = config.splatoon.brands.find(function(arg) {
            return arg.name.toLowerCase() == lower_case_input;
        });

        if(!brand) {
            bot.sendMessage({ to: opts.chatID, message: 'Could not find brand "' + input + '".' });
            return;
        }

        var message = '';

        if(brand.buffed == null || brand.nerfed == null) {
            message = 'The brand "' + brand.name + '" is neutral!\n';
        }
        else {
            message = 'The brand "' + brand.name + '" has ';
            message = message.concat('buffed ', config.splatoon.abilities[brand.buffed - 1], ' and nerfed ', config.splatoon.abilities[brand.nerfed - 1], ' probabilities\n');
        }

        bot.sendMessage({ to: opts.chatID, message: message });
    }
};

commands.reloadconfig = {
    authority: 3,
    command: function(opts) {
        load_config();
    }
};

function message(user, userID, chatID, message, rawEvent) {
    console.log(user + ' said: ' + message);
    var words = message.split(' ');
    var prefix = words[0];
    opts = { user: user, userID: userID, message: message, rawEvent: rawEvent, chatID: chatID, words: words.slice(1) };
    var command = get_command_from_message(prefix);

    if(command) {
        if(typeof(command) == 'object') {
            var authority_required = command.authority || 0;
            if(get_user_authority(userID) >= authority_required) {
                command.command(opts);
            }
            else {
                bot.sendMessage({ to: opts.chatID, message: "Sorry, you're not authorised to use this command" });
            }
        }
        else {
            command(opts);
        }
    }
}

bot.on('message', message);
bot.on('ready', function(rawEvent) {
    console.log('Connected!\nLogging in as: ');
    console.log(bot.username);
    console.log(bot.id);
    console.log('-----');
});
