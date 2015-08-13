var Discord = require('discord.js');
var request = require('request');
var fs = require('fs');

var config = {};

function load_config() {
    config = JSON.parse(fs.readFileSync('config.json').toString());
}

function save_config() {
    fs.writeFileSync('config.json', JSON.stringify(config, null, 4), 'utf-8');
}

load_config();

var bot = new Discord.Client();
bot.login(config.username, config.password);

var commands = {};

var authority_prettify = {
    0: "User",
    1: "Moderator",
    2: "Admin",
    3: "Creator"
};


function find_from(list, predicate) {
    for(var i = 0; i < list.length; ++i) {
        var value = list[i];
        if(predicate(value)) {
            return value;
        }
    }
    return null;
}

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
    command: function(message) {
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
        bot.sendMessage(message.channel, text);
    }
};

commands.hello = {
    help: 'displays my intro message!',
    command: function(message) {
        bot.sendMessage(message.channel, "Hello! I'm a robot! Danny made me.");
    }
};

commands.random = {
    help: 'displays a random weapon, map, or number',
    command: function(message) {
        var error_string = 'Random what? weapon, map, or number? (e.g. !random weapon)'
        if(message.args.length < 1) {
            bot.sendMessage(message.channel, error_string);
            return;
        }

        var type = message.args[0].toLowerCase();
        if(type == 'weapon') {
            var index = Math.floor(Math.random() * config.splatoon.weapons.length);
            bot.sendMessage(message.channel, config.splatoon.weapons[index]);
        }
        else if(type == 'number') {
            bot.sendMessage(message.channel, Math.floor(Math.random() * 100).toString());
        }
        else if(type == 'map') {
            var index = Math.floor(Math.random() * config.splatoon.maps.length);
            bot.sendMessage(message.channel, config.splatoon.maps[index]);
        }
        else {
            bot.sendMessage(message.channel, error_string);
        }
    }
};

function get_splatoon_map_callback(index, prefix, current_channel) {
    return function(error, response, body) {
        if(error || response.statusCode != 200) {
            bot.sendMessage(current_channel, "Unfortunately an error occurred. Tell Danny the error was " + error);
            return;
        }

        var data = JSON.parse(body);
        var schedule = data['schedule'];
        if(!schedule || schedule.length == 0) {
            bot.sendMessage(current_channel, "Maps could not be found...");
            return;
        }

        var current_maps = schedule[index];
        var result = '';
        result = result.concat(prefix, ' regular maps: ', current_maps.regular.maps[0].nameEN, ' and ', current_maps.regular.maps[1].nameEN, '\n');
        result = result.concat(prefix, ' ', current_maps.ranked.rulesEN, ' maps: ', current_maps.ranked.maps[0].nameEN, ' and ', current_maps.ranked.maps[1].nameEN);
        bot.sendMessage(current_channel, result);
    };
}

function to_percent(value) {
    return (value * 100).toString() + '%';
}

commands.maps = {
    help: 'shows the current Splatoon maps in rotation',
    command: function(message) {
        request(config.splatoon.schedule_url, get_splatoon_map_callback(0, 'Current', message.channel));
    }
};

commands.nextmaps = {
    help: 'shows the next Splatoon maps in rotation',
    command: function(message) {
        request(config.splatoon.schedule_url, get_splatoon_map_callback(1, 'Next', message.channel));
    }
};

commands.quit = {
    authority: 3,
    command: function(message) {
        bot.disconnect();
        process.exit(0);
    }
};

commands.choose = {
    help: 'helps choose between multiple choices',
    command: function(message) {
        if(message.args.length < 2) {
            bot.sendMessage(message.channel, 'Not enough choices to choose from... (e.g. !choose 1 2 3)');
            return;
        }

        var random_index = Math.floor(Math.random() * message.args.length);
        bot.sendMessage(message.channel, message.args[random_index]);
    }
};

commands.brand = {
    help: 'shows info about a splatoon brand',
    command: function(message) {
        var input = message.args.join(' ');
        var lower_case_input = input.toLowerCase();

        if(lower_case_input == 'list') {
            bot.sendMessage(message.channel, config.splatoon.brands.map(function(arg) { return arg.name; }).join(', '));
            return;
        }
        var brand = find_from(config.splatoon.brands, function(arg) {
            return arg.name.toLowerCase() == lower_case_input;
        });

        if(!brand) {
            bot.sendMessage(message.channel, 'Could not find brand "' + input + '".');
            return;
        }

        var result = '';

        if(brand.buffed == null || brand.nerfed == null) {
            result = 'The brand "' + brand.name + '" is neutral!\n';
        }
        else {
            result = 'The brand "' + brand.name + '" has ';
            result = result.concat('buffed ', config.splatoon.abilities[brand.buffed - 1], ' and nerfed ', config.splatoon.abilities[brand.nerfed - 1], ' probabilities\n');
        }

        bot.sendMessage(message.channel, result);
    }
};

commands.reloadconfig = {
    authority: 3,
    command: function(message) {
        load_config();
    }
};

commands.info = {
    help: 'shows information about the current user or another user',
    command: function(message) {
        var server = message.channel.server;
        var user = server.members.filter('username', message.args.join(' '), true) || message.author;
        var owner = server.members.filter('id', server.ownerID, true).username;
        var authority = get_user_authority(user.id);
        var text = 'Info for ' + user.mention() + ':\nYou\'re currently in #' + message.channel.name + ' in server ' + server.name;
        text = text.concat(' (', server.region, ')\n', 'The owner of this group is ', owner, '\n', 'Your Discord ID is: ', user.id);
        text = text.concat('\nYour authority on me is **' + authority_prettify[authority] + '**');
        bot.sendMessage(message.channel, text);
    }
};

commands.cleanup = {
    help: 'cleans up past messages',
    authority: 1,
    command: function(message) {
        var amount = parseInt(message.args[0]) || 100;
        var text = '';
        var count = 0;
        var done;
        bot.getChannelLogs(message.channel, amount, function(logs) {
            for(message of logs.contents) {
                if(message.author.id === bot.user.id) {
                    ++count;
                    bot.deleteMessage(message);
                }
            }
            bot.deleteMessage(done);
            bot.sendMessage(message.channel, 'Cleanup has completed. ' + count + ' messages were deleted', false, true, { selfDestruct: 3000 });
        });
        bot.sendMessage(message.channel, 'Cleaning up...', function(arg) { done = arg; });
    }
}

commands.authority = {
    help: 'manages the authority of a user',
    authority: 1,
    command: function(message) {
        var server = message.channel.server;
        var authority = parseInt(message.args[0]) || 0;
        var author_authority = get_user_authority(message.author.id);
        var user = server.members.filter('username', message.args.slice(1).join(' '), true);

        if(!user) {
            bot.sendMessage(message.channel, 'User not found');
            return;
        }

        if(authority > author_authority || author_authority < get_user_authority(user.id)) {
            bot.sendMessage(message.channel, "You can't give someone authority higher than yours.");
            return;
        }

        config.authority[user.id] = authority;
        save_config();
        bot.sendMessage(message.channel, user.username + ' now has an authority of **' + authority_prettify[authority] + '**');
    }
}

function message_callback(message) {
    // console.log(user + ' said: ' + message);
    var words = message.content.split(' ');
    var prefix = words[0];
    var command = get_command_from_message(prefix);

    if(command) {
        message.args = words.slice(1);
        if(typeof(command) == 'object') {
            var authority_required = command.authority || 0;
            if(get_user_authority(message.author.id) >= authority_required) {
                command.command(message);
            }
            else {
                bot.sendMessage(message.channel, "Sorry, you're not authorised to use this command");
            }
        }
        else {
            command(message);
        }
    }
}

bot.on('message', message_callback);
bot.on('ready', function() {
    console.log('Connected!\nLogging in as: ');
    console.log(bot.user.username);
    console.log(bot.user.id);
    console.log('-----');
});
